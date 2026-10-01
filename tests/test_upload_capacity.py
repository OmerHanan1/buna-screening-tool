"""Real image-bearing PDFs exercise byte limits independently of text/page limits."""
import asyncio
import hashlib
import inspect
import json
import shutil
import threading
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from fastapi import HTTPException
import pytest
from starlette.datastructures import Headers
from starlette.requests import ClientDisconnect

from buna.documents import extract_document, ExtractionError
from buna.public_app import create_public_app, PublicBodyLimit
from buna.upload_limits import FileLimitedParser, MAX_FILE_BYTES, REQUEST_BYTES
from test_public_app import corpus, worker
from test_public_bulk import stage
from test_public_source_saves import upload, wait
from test_shared_library import MemoryBlobs
from test_shared_library_api import session, setup
from buna.public_source_worker import validate_source
from deploy.verify_upload_capacity import image_pdf


@pytest.fixture(scope="module")
def large_pdfs(tmp_path_factory):
    root = tmp_path_factory.mktemp("original-synthetic-image-pdfs")
    result = {}
    for mib in (36, 40):
        path = root / f"synthetic-{mib}.pdf"
        image_pdf(path, mib * 1024 * 1024)
        result[mib] = path
    return result


@pytest.mark.parametrize("mib", [36, 40])
@pytest.mark.parametrize("profile", ["manuscript", "source"])
def test_real_pdf_parser_boundary(large_pdfs, mib, profile):
    document = extract_document(large_pdfs[mib], profile=profile)
    assert len(document["pages"]) == 8
    assert document["extraction"]["truncated"] is False
    for number, page in enumerate(document["pages"], 1):
        assert page["number"] == number
        assert f"synthetic sensor study page {number}" in page["text"]
    assert "\n\n".join(page["text"] for page in document["pages"]) == document["text"]


def test_parser_rejects_one_byte_over_before_reading(tmp_path, large_pdfs):
    path = tmp_path / "over.pdf"
    with large_pdfs[40].open("rb") as source, path.open("wb") as output:
        shutil.copyfileobj(source, output, 65536)
        output.write(b"\n")
    for profile in ("manuscript", "source"):
        with pytest.raises(ExtractionError, match="40 MiB"):
            extract_document(path, profile=profile)


def test_exact_source_save_persistence_sha_and_duplicate(tmp_path, monkeypatch, large_pdfs):
    setup(monkeypatch)
    root, _ = corpus(tmp_path)
    blobs = MemoryBlobs()
    parsed = []
    def parser(folder, uid):
        parsed.append(uid)
        validate_source(folder)
    app = create_public_app(root, tmp_path, worker_runner=worker, shared_store=blobs, source_runner=parser)
    with TestClient(app) as client:
        headers = session(client)
        with large_pdfs[40].open("rb") as source:
            response = upload(client, headers, source)
        assert response.status_code == 202, response.text
        saved = wait(client, response.json()["id"], headers)
        assert saved["state"] == "saved", saved
        with large_pdfs[40].open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        assert saved["digest"] == digest
        paper = next(p for p in app.state.shared_library.list() if p["sha256"] == digest)
        original_name, cache_name = app.state.shared_library._object_paths(paper)
        assert len(blobs.values[original_name][0]) == MAX_FILE_BYTES
        assert hashlib.sha256(blobs.values[original_name][0]).hexdigest() == digest
        cached = json.loads(blobs.values[cache_name][0])
        assert cached["extraction"]["pages_extracted"] == 8
        with large_pdfs[40].open("rb") as source:
            duplicate = upload(client, headers, source)
        assert wait(client, duplicate.json()["id"], headers)["state"] == "already-present"
        assert len(parsed) == 1
    restart = tmp_path / "restart"
    restart.mkdir()
    with TestClient(create_public_app(root, restart, worker_runner=worker, shared_store=blobs)) as client:
        assert digest in {p["sha256"] for p in client.get("/api/public/library", headers=session(client)).json()["papers"]}


def test_three_large_private_sources_and_target_snapshot(tmp_path, monkeypatch, large_pdfs):
    setup(monkeypatch)
    root, _ = corpus(tmp_path)
    finished = threading.Event()
    captured = {}
    def check(folder, uid):
        request = json.loads((folder / "request.json").read_text())
        assert (folder / request["target"]).stat().st_size == MAX_FILE_BYTES
        assert extract_document(folder / request["target"])["extraction"]["pages_extracted"] == 8
        for entry in request["sources"]:
            path = folder / entry["path"]
            assert path.stat().st_size == MAX_FILE_BYTES
            assert extract_document(path, profile="source")["extraction"]["pages_extracted"] == 8
        captured.update(request)
        worker(folder, uid)
        finished.set()
    with TestClient(create_public_app(root, tmp_path, worker_runner=check)) as client:
        headers = session(client)
        refs = []
        for index in range(3):
            distinct = tmp_path / "distinct.pdf"
            shutil.copyfile(large_pdfs[40], distinct)
            with distinct.open("r+b") as stream:
                stream.seek(-64, 2)
                stream.write(str(index).encode())
            with distinct.open("rb") as source:
                response = stage(client, headers, source, f"source-{index}.pdf")
            assert response.status_code == 201, response.text
            refs.append(response.json()["id"])
        with large_pdfs[40].open("rb") as source:
            rejected = stage(client, headers, source, "over-batch.pdf")
        assert rejected.status_code == 413 and "128 MiB" in rejected.text
        assert stage(client, headers, b"x" * (8 * 1024 * 1024), "remaining-budget.txt").status_code == 201
        assert stage(client, headers, b"x", "one-byte-over-budget.txt").status_code == 413
        with large_pdfs[40].open("rb") as target:
            response = client.post("/api/public/jobs", headers=headers,
                                   data={"uploaded_sources": json.dumps(refs)},
                                   files={"target": ("target.pdf", target, "application/pdf")})
        assert response.status_code == 202, response.text
        assert finished.wait(20)
        assert len(captured["sources"]) == 3
        foreign = session(client)
        assert client.delete("/api/public/source-uploads/" + refs[0], headers=foreign).status_code == 404
        assert client.delete("/api/public/source-uploads", headers=headers).json()["count"] == 4


@pytest.mark.parametrize("endpoint,field", [("jobs", "target"), ("source-uploads", "source"), ("source-saves", "source")])
def test_actual_byte_overage_cleans_admission(tmp_path, monkeypatch, endpoint, field):
    setup(monkeypatch)
    root, _ = corpus(tmp_path)
    app = create_public_app(root, tmp_path, worker_runner=worker, shared_store=MemoryBlobs())
    with TestClient(app) as client:
        headers = {**session(client), "Idempotency-Key": str(uuid4())}
        response = client.post("/api/public/" + endpoint, headers=headers,
                               data={"share_authorized": "true"} if endpoint == "source-saves" else {},
                               files={field: ("oversized.pdf", b"%PDF-" + b"x" * (MAX_FILE_BYTES - 4))})
        assert response.status_code == 413 and "40 MiB" in response.text
        route = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/public/" + endpoint and "POST" in r.methods)
        variables = inspect.getclosurevars(route).nonlocals
        with variables["connect"]() as db:
            table = endpoint.replace("-", "_")
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        runtime = variables["root"]
        assert not list(runtime.rglob("original.pdf"))
        assert not list(runtime.glob("source-uploads/*"))
        assert not list(runtime.glob("source-saves/*"))


@pytest.mark.parametrize("failure", ["over-file", "disconnect", "cancel", "over-body"])
def test_multipart_stream_errors_close_spools(monkeypatch, failure):
    import buna.upload_limits as limits
    monkeypatch.setattr(limits, "MAX_FILE_BYTES", 2 * 1024 * 1024)
    async def chunks():
        yield b'--boundary\r\nContent-Disposition: form-data; name="source"; filename="a.pdf"\r\n\r\n'
        for _ in range(32):
            yield b"x" * 65536
        if failure == "disconnect":
            raise ClientDisconnect()
        if failure == "cancel":
            raise asyncio.CancelledError()
        if failure == "over-body":
            raise HTTPException(413, "Upload exceeds the request limit.")
        yield b"x"
    parser = FileLimitedParser(Headers({"content-type": "multipart/form-data; boundary=boundary"}), chunks())
    with pytest.raises((HTTPException, ClientDisconnect, asyncio.CancelledError)):
        asyncio.run(parser.parse())
    assert parser._files_to_close_on_error
    assert all(file.closed for file in parser._files_to_close_on_error)


@pytest.mark.parametrize("declared", [None, b"1", str(REQUEST_BYTES).encode()])
def test_body_limit_counts_actual_stream_bytes(monkeypatch, declared):
    import buna.public_app as gateway
    monkeypatch.setattr(gateway, "BODY_LIMIT", 100)
    consumed = 0
    async def receive():
        nonlocal consumed
        consumed += 1
        return {"type": "http.request", "body": b"x" * 25, "more_body": True}
    async def inner(scope, receive, send):
        while True:
            await receive()
    async def send(message):
        assert message.get("status", 413) == 413
    headers = [] if declared is None else [(b"content-length", declared)]
    if declared == str(REQUEST_BYTES).encode():
        asyncio.run(PublicBodyLimit(inner)({"type": "http", "headers": headers}, receive, send))
        assert consumed == 0
    else:
        with pytest.raises(HTTPException) as error:
            asyncio.run(PublicBodyLimit(inner)({"type": "http", "headers": headers}, receive, send))
        assert error.value.status_code == 413
        assert consumed == 5
