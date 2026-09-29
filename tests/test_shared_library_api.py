import hashlib
import json
import time
import inspect
import threading

from fastapi.testclient import TestClient
import pymupdf
import pytest

from buna.app import create_app
from buna.documents import extract_document
from buna.public_app import create_public_app
from test_public_app import corpus, worker
from test_shared_library import MemoryBlobs


def pdf(text):
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(45, 45, 550, 750), text, fontsize=10)
        return document.tobytes()


SOURCE = pdf("Introduction\nAmber birds gather beside quiet rivers during winter mornings.\nDiscussion\nOriginal source conclusions complete this synthetic paper.")
TARGET = pdf("Abstract\nA completely separate synthetic manuscript studies ceramic sensors and stable laboratory measurements.")


def validating_worker(folder, uid):
    request = json.loads((folder / "request.json").read_text())
    validation = {}
    for i, entry in enumerate(request["sources"], 1):
        if entry.get("keep_in_library") and not entry.get("cached_document"):
            document = extract_document(folder / entry["path"], profile="source")
            (folder / f"parsed-source-{i}.json").write_text(json.dumps(document))
            validation[str(i)] = {"validated": True}
    (folder / "share-validation.json").write_text(json.dumps(validation))
    worker(folder, uid)


def session(client):
    return {"Authorization": "Bearer " + client.post("/api/public/session", json={"email": "owner@example.org"}).json()["token"]}


def finish(client, job, headers):
    for _ in range(200):
        state = client.get("/api/public/jobs/" + job, headers=headers).json()
        if state["status"] != "running" and not any(s["state"] in {"pending", "saving"} for s in state["library_saves"]):
            return state
        time.sleep(.01)
    pytest.fail("Shared saving did not settle")


def setup(monkeypatch):
    monkeypatch.setenv("BUNA_ACCESS_MODE", "email-gate")
    monkeypatch.setenv("BUNA_ALLOWED_EMAIL", "owner@example.org")


def test_optin_source_only_survives_app_restart_and_another_visitor(tmp_path, monkeypatch):
    setup(monkeypatch)
    root, curated = corpus(tmp_path)
    blobs = MemoryBlobs()
    with TestClient(create_public_app(root, tmp_path, worker_runner=validating_worker, shared_store=blobs)) as c:
        first = session(c)
        before = c.get("/api/public/library", headers=first).json()
        assert before["shared_saving_available"] and len(before["papers"]) == 1
        response = c.post("/api/public/jobs", headers=first,
                          data={"selected": "[]", "save_sources": "[0]", "share_authorized": "true"},
                          files=[("target", ("manuscript.pdf", TARGET)), ("sources", ("Source.pdf", SOURCE))])
        assert response.status_code == 202, response.text
        job = response.json()["id"]
        state = finish(c, job, first)
        assert state["status"] == "complete" and state["library_saves"][0]["state"] == "saved"
        assert hashlib.sha256(TARGET).hexdigest() not in "".join(blobs.values)
        assert c.post("/api/public/jobs/" + job + "/library-save-retry", headers=session(c)).status_code == 404
    with TestClient(create_public_app(root, tmp_path, worker_runner=validating_worker, shared_store=blobs)) as c:
        second = session(c)
        papers = c.get("/api/public/library", headers=second).json()["papers"]
        assert {p["sha256"] for p in papers} == {curated["sha256"], hashlib.sha256(SOURCE).hexdigest()}
        assert c.get("/api/public/jobs/" + job, headers=second).status_code == 404
        assert c.get("/api/public/sources/" + hashlib.sha256(SOURCE).hexdigest(), headers=second).status_code == 404
        response = c.post("/api/public/jobs", headers=second,
                          data={"selected": json.dumps([hashlib.sha256(SOURCE).hexdigest()])},
                          files={"target": ("new-target.pdf", TARGET)})
        assert response.status_code == 202
        assert finish(c, response.json()["id"], second)["status"] == "complete"


def test_unchecked_and_manuscript_copy_are_not_shared(tmp_path, monkeypatch):
    setup(monkeypatch)
    root, _ = corpus(tmp_path)
    blobs = MemoryBlobs()
    with TestClient(create_public_app(root, tmp_path, worker_runner=validating_worker, shared_store=blobs)) as c:
        headers = session(c)
        for fields, source in [({"selected": "[]"}, SOURCE),
                               ({"selected": "[]", "save_sources": "[0]", "share_authorized": "true"}, TARGET)]:
            response = c.post("/api/public/jobs", headers=headers, data=fields,
                              files=[("target", ("manuscript.pdf", TARGET)), ("sources", ("comparison.pdf", source))])
            assert response.status_code == 202
            state = finish(c, response.json()["id"], headers)
            assert all(s["state"] == "rejected" for s in state["library_saves"])
        assert not blobs.values


def test_save_outage_does_not_hide_pdf_and_retry_does_not_recompare(tmp_path, monkeypatch):
    setup(monkeypatch)
    root, _ = corpus(tmp_path)
    blobs = MemoryBlobs()
    blobs.fail_objects = True
    runs = []
    def tracked_worker(folder, uid):
        runs.append(str(folder))
        validating_worker(folder, uid)
    with TestClient(create_public_app(root, tmp_path, worker_runner=tracked_worker, shared_store=blobs)) as c:
        headers = session(c)
        response = c.post("/api/public/jobs", headers=headers,
                          data={"selected": "[]", "save_sources": "[0]", "share_authorized": "true"},
                          files=[("target", ("manuscript.pdf", TARGET)), ("sources", ("source.pdf", SOURCE))])
        job = response.json()["id"]
        state = finish(c, job, headers)
        assert state["status"] == "complete" and state["library_saves"][0]["state"] == "failed"
        assert c.get(f"/api/public/jobs/{job}/report.pdf", headers=headers).content.startswith(b"%PDF-")
        blobs.fail_objects = False
        assert c.post(f"/api/public/jobs/{job}/library-save-retry", headers=headers).status_code == 202
        state = finish(c, job, headers)
        assert state["library_saves"][0]["state"] == "saved"
        assert len(runs) == 1


@pytest.mark.parametrize("save_indices,authorization", [("[0]", ""), ("[-1]", "true"), ("[true]", "true"), ("[9]", "true")])
def test_invalid_or_unattested_save_selection_is_rejected(tmp_path, monkeypatch, save_indices, authorization):
    setup(monkeypatch)
    root, _ = corpus(tmp_path)
    blobs = MemoryBlobs()
    with TestClient(create_public_app(root, tmp_path, worker_runner=validating_worker, shared_store=blobs)) as c:
        response = c.post("/api/public/jobs", headers=session(c),
                          data={"selected": "[]", "save_sources": save_indices, "share_authorized": authorization},
                          files=[("target", ("manuscript.pdf", TARGET)), ("sources", ("source.pdf", SOURCE))])
        assert response.status_code == 422
        assert not blobs.values


def test_cancellation_and_shared_publication_have_atomic_order(tmp_path, monkeypatch):
    """Force the old stale-status interleaving without killing a real process."""
    setup(monkeypatch)
    root, _ = corpus(tmp_path)
    blobs = MemoryBlobs()
    worker_ready, release_worker, publication_started, release_publication = (threading.Event() for _ in range(4))
    original_immutable = blobs.immutable
    def blocked_upload(name, data):
        publication_started.set()
        assert release_publication.wait(5)
        original_immutable(name, data)
    blobs.immutable = blocked_upload
    def paused_worker(folder, uid):
        validating_worker(folder, uid)
        worker_ready.set()
        assert release_worker.wait(5)
    app = create_public_app(root, tmp_path, worker_runner=paused_worker, shared_store=blobs)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/public/jobs/{job_id}" and "DELETE" in getattr(route, "methods", set()))
    cells = dict(zip(endpoint.__code__.co_freevars, endpoint.__closure__))
    closure = inspect.getclosurevars(endpoint).nonlocals
    original_owned = cells["owned"].cell_contents
    def raced_owned(job_id, owner):
        row = original_owned(job_id, owner)
        release_worker.set()
        if not closure["lock"]._is_owned():
            assert publication_started.wait(5)
        return row
    monkeypatch.setattr("buna.public_app.os.killpg", lambda *_: None)
    with TestClient(app) as c:
        headers = session(c)
        response = c.post("/api/public/jobs", headers=headers,
                          data={"selected": "[]", "save_sources": "[0]", "share_authorized": "true"},
                          files=[("target", ("manuscript.pdf", TARGET)), ("sources", ("source.pdf", SOURCE))])
        job = response.json()["id"]
        assert worker_ready.wait(5)
        with closure["lock"]:
            closure["active"][job] = type("FakeProcess", (), {"pid": -1})()
        cells["owned"].cell_contents = raced_owned
        try:
            cancelled = c.delete("/api/public/jobs/" + job, headers=headers)
            assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        finally:
            cells["owned"].cell_contents = original_owned
            release_publication.set()
            release_worker.set()
        state = finish(c, job, headers)
        assert state["library_saves"][0]["state"] == "cancelled"
        assert not publication_started.is_set()
        assert not blobs.values
