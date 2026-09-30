import hashlib
import inspect
import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from buna.public_app import create_public_app
from test_public_app import corpus, worker
from test_public_source_saves import app_for, upload, wait
from test_shared_library import MemoryBlobs
from test_shared_library_api import pdf, session, setup


def stage(client, headers, content=b"Original synthetic source.", name="same-name.txt", key=None):
    return client.post("/api/public/source-uploads", headers={**headers, "Idempotency-Key": key or str(uuid4())},
                       files={"source": (name, content)})


def job(client, headers, references, selected=None):
    return client.post("/api/public/jobs", headers=headers,
                       data={"uploaded_sources": json.dumps(references), "selected": json.dumps(selected or [])},
                       files={"target": ("target.txt", b"Original synthetic manuscript.")})


def rate_clock(monkeypatch):
    import buna.public_app as gateway
    now = [time.time()]
    monkeypatch.setattr(gateway, "time", SimpleNamespace(time=lambda: now[0], monotonic=time.monotonic, sleep=time.sleep))
    return now


def test_fifty_private_uploads_one_snapshot_no_sharing(tmp_path, monkeypatch):
    setup(monkeypatch)
    root, paper = corpus(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    for i in range(43):
        content = f"Original synthetic curated source {i}.".encode()
        digest = hashlib.sha256(content).hexdigest()
        (root / f"{digest}.txt").write_bytes(content)
        manifest["papers"].append({**paper, "sha256": digest, "filename": f"{digest}.txt", "title": f"Curated {i}"})
    (root / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr("buna.public_app.load_corpus", lambda _: manifest["papers"])
    captured = []
    finished = threading.Event()
    def record(folder, uid):
        value = json.loads((folder / "request.json").read_text())
        captured.extend(value["sources"])
        worker(folder, uid)
        (folder / "complete.json").write_text(json.dumps({"checked": len(captured), "total": len(captured)}))
        finished.set()
    blobs = MemoryBlobs()
    with TestClient(create_public_app(root, tmp_path, worker_runner=record, shared_store=blobs)) as client:
        headers = session(client)
        references = []
        for i in range(50):
            response = stage(client, headers, pdf(f"Introduction\nOriginal synthetic study number {i}.\nDiscussion\nUnique experimental observations."), "same-name.pdf")
            assert response.status_code == 201, response.text
            references.append(response.json()["id"])
        assert stage(client, headers).status_code == 422
        response = job(client, headers, references, [p["sha256"] for p in manifest["papers"]])
        assert response.status_code == 202, response.text
        assert response.json()["source_count"] == 94
        assert finished.wait(5)
        assert len(captured) == 94
        assert len({entry["sha256"] for entry in captured if "sha256" in entry}) == 50
        assert not blobs.values
        # Clearing originals does not alter copied job files or another visitor's uploads.
        other = session(client)
        foreign = stage(client, other).json()["id"]
        assert client.delete("/api/public/source-uploads", headers=headers).json()["count"] == 50
        assert job(client, headers, [foreign]).status_code == 404
        assert stage(client, headers).status_code == 201


@pytest.mark.parametrize("count", [5, 6, 49, 50, 51])
def test_inline_count_boundary(count, tmp_path):
    root, _ = corpus(tmp_path)
    with TestClient(create_public_app(root, tmp_path, worker_runner=worker)) as client:
        headers = {"Authorization": "Bearer " + client.post("/api/public/session").json()["token"]}
        response = client.post("/api/public/jobs", headers=headers,
                               files=[("target", ("a.txt", b"target"))] +
                               [("sources", (f"{i}.txt", str(i).encode())) for i in range(count)])
        assert response.status_code == (202 if count <= 50 else 400), response.text
        if count <= 50:
            assert response.json()["source_count"] == count


def test_private_ownership_idempotency_expiry_and_content_dedup(tmp_path, monkeypatch):
    setup(monkeypatch)
    root, _ = corpus(tmp_path)
    with TestClient(create_public_app(root, tmp_path, worker_runner=worker)) as client:
        first, second = session(client), session(client)
        key = str(uuid4())
        reference = stage(client, first, key=key).json()["id"]
        assert stage(client, first, key=key).json()["id"] == reference
        assert client.get("/api/public/source-uploads/" + reference, headers=first).status_code == 405
        assert client.delete("/api/public/source-uploads/" + reference, headers=second).status_code == 404
        assert job(client, second, [reference]).status_code == 404
        assert job(client, first, [reference, reference]).status_code == 422
        duplicate = stage(client, first).json()["id"]
        different = stage(client, first, b"Distinct content but same name.").json()["id"]
        response = job(client, first, [reference, duplicate, different])
        assert response.status_code == 202 and response.json()["source_count"] == 2
        import buna.public_source_uploads as uploads
        monkeypatch.setattr(uploads, "RETENTION", -1)
        assert job(client, first, [reference]).status_code in {404, 429}
        assert client.delete("/api/public/source-uploads/" + reference, headers=first).status_code == 404


def test_upload_errors_release_reservations_and_size_limits(tmp_path, monkeypatch):
    root, _ = corpus(tmp_path)
    with TestClient(create_public_app(root, tmp_path, worker_runner=worker)) as client:
        headers = {"Authorization": "Bearer " + client.post("/api/public/session").json()["token"]}
        for body, name, status in [(b"", "a.txt", 422), (b"not pdf", "a.pdf", 422), (b"zip", "a.zip", 415),
                                   (b"x" * (8 * 1024 * 1024 + 1), "a.txt", 413)]:
            response = stage(client, headers, body, name)
            assert response.status_code == status, response.text
        assert client.delete("/api/public/source-uploads", headers=headers).json()["count"] == 0
        import buna.public_source_uploads as uploads
        monkeypatch.setattr(uploads, "MAX_BATCH_BYTES", 8 * 1024 * 1024)
        assert stage(client, headers, b"x" * (8 * 1024 * 1024)).status_code == 201
        response = stage(client, headers)
        assert response.status_code == 413 and "128 MiB" in response.text
        assert client.delete("/api/public/source-uploads", headers=headers).status_code == 200
        monkeypatch.setattr(uploads, "MAX_BATCH_BYTES", 8)
        assert stage(client, headers, b"1234").status_code == 201
        assert stage(client, headers, b"5678").status_code == 201
        assert stage(client, headers, b"9").status_code == 413
        monkeypatch.setattr(uploads, "job_storage_bytes", lambda root: 384 * 1024 * 1024)
        assert client.delete("/api/public/source-uploads", headers=headers).status_code == 200
        response = stage(client, headers)
        assert response.status_code == 429 and response.headers["Retry-After"] == "10"


def test_fifty_sequential_saves_durable_and_parser_single(tmp_path, monkeypatch):
    clock = rate_clock(monkeypatch)
    blobs = MemoryBlobs()
    current = peak = 0
    from buna.public_source_worker import validate_source
    def parse(folder, uid):
        nonlocal current, peak
        current += 1
        peak = max(peak, current)
        try:
            validate_source(folder)
        finally:
            current -= 1
    app = app_for(tmp_path, monkeypatch, blobs, parse)
    with TestClient(app) as client:
        headers = session(client)
        digests = []
        for i in range(50):
            clock[0] += 6
            content = pdf(f"Introduction\nOriginal amber research sample {i}.\nDiscussion\nMethods and findings are synthetic.")
            response = upload(client, headers, content)
            assert response.status_code == 202, response.text
            result = wait(client, response.json()["id"], headers)
            assert result["state"] == "saved", result
            digests.append(result["digest"])
        assert peak == 1
        other = session(client)
        assert set(digests) <= {p["sha256"] for p in client.get("/api/public/library", headers=other).json()["papers"]}
        rejected = upload(client, headers, pdf("Introduction\nOne extra source beyond daily admissions.\nDiscussion\nOriginal synthetic conclusion."))
        assert wait(client, rejected.json()["id"], headers)["state"] == "failed"
        assert len(app.state.shared_library.list()) == 50
        assert len(json.loads(blobs.values["catalog-v1.json"][0])["reservations"]) == 50


def test_daily_parse_budget_rejects_before_parser(tmp_path, monkeypatch):
    blobs = MemoryBlobs()
    app = app_for(tmp_path, monkeypatch, blobs, lambda *_: pytest.fail("Parser budget exhausted"))
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/public/source-saves" and "POST" in route.methods)
    connect = inspect.getclosurevars(endpoint).nonlocals["connect"]
    with connect() as db:
        db.execute("INSERT INTO source_parse_budget VALUES('spent',?,2250)", (time.time(),))
    with TestClient(app) as client:
        headers = session(client)
        result = upload(client, headers)
        failure = wait(client, result.json()["id"], headers)
        assert failure["state"] == "failed" and "parsing time budget" in failure["reason"]
