import hashlib
import json
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from buna.public_app import create_public_app
from buna.public_source_worker import validate_source
from test_public_app import corpus
from test_shared_library import MemoryBlobs
from test_shared_library_api import SOURCE, TARGET, session, setup


def parse(folder, uid):
    validate_source(folder)


def upload(client, headers, data=SOURCE, key=None, authorization="true"):
    return client.post("/api/public/source-saves",
                       headers={**headers, "Idempotency-Key": key or str(uuid4())},
                       data={"share_authorized": authorization},
                       files={"source": ("source.pdf", data, "application/pdf")})


def wait(client, receipt, headers):
    for _ in range(500):
        value = client.get("/api/public/source-saves/" + receipt, headers=headers).json()
        if value["state"] in {"saved", "already-present", "failed", "cancelled"}:
            return value
        time.sleep(.01)
    pytest.fail("Save did not finish")


def app_for(tmp_path, monkeypatch, blobs, runner=parse):
    setup(monkeypatch)
    tmp_path.mkdir(parents=True, exist_ok=True)
    root, _ = corpus(tmp_path)
    return create_public_app(root, tmp_path, worker_runner=lambda *_: pytest.fail("No comparison requested"),
                             shared_store=blobs, source_runner=runner)


def test_source_alone_saved_visible_restart_and_owner_isolation(tmp_path, monkeypatch):
    blobs = MemoryBlobs()
    app = app_for(tmp_path, monkeypatch, blobs)
    with TestClient(app) as client:
        first = session(client)
        key = str(uuid4())
        response = upload(client, first, key=key)
        assert response.status_code == 202, response.text
        receipt = response.json()["id"]
        saved = wait(client, receipt, first)
        assert saved["state"] == "saved"
        digest = hashlib.sha256(SOURCE).hexdigest()
        assert saved["digest"] == digest
        assert upload(client, first, key=key).json()["id"] == receipt
        other = session(client)
        assert client.get("/api/public/source-saves/" + receipt, headers=other).status_code == 404
        assert client.post("/api/public/source-saves/" + receipt + "/retry", headers=other).status_code == 404
        assert client.delete("/api/public/source-saves/" + receipt, headers=other).status_code == 404
        assert digest in {p["sha256"] for p in client.get("/api/public/library", headers=other).json()["papers"]}
        assert client.get("/api/public/sources/" + digest, headers=first).status_code == 404
        duplicate = upload(client, first)
        assert wait(client, duplicate.json()["id"], first)["state"] == "already-present"
        assert client.delete("/api/public/source-saves/" + receipt, headers=first).status_code == 409
    # A new gateway loses visitor receipts, but not saved library membership.
    with TestClient(app_for(tmp_path / "restart", monkeypatch, blobs)) as client:
        headers = session(client)
        assert digest in {p["sha256"] for p in client.get("/api/public/library", headers=headers).json()["papers"]}
        assert client.get("/api/public/source-saves/" + receipt, headers=headers).status_code == 404
    catalog = json.loads(blobs.values["catalog-v1.json"][0])
    event = next(e for e in catalog["save_events"] if e["id"] == receipt)
    assert event["state"] == "saved" and set(event) == {"id", "time", "state", "code"}
    assert hashlib.sha256(TARGET).hexdigest() not in str(blobs.values)


def test_storage_failure_retry_uses_retained_original_no_comparison(tmp_path, monkeypatch):
    blobs = MemoryBlobs()
    blobs.fail_objects = True
    with TestClient(app_for(tmp_path, monkeypatch, blobs)) as client:
        headers = session(client)
        receipt = upload(client, headers).json()["id"]
        assert wait(client, receipt, headers)["state"] == "failed"
        blobs.fail_objects = False
        assert client.post("/api/public/source-saves/" + receipt + "/retry", headers=headers).status_code == 202
        assert wait(client, receipt, headers)["state"] == "saved"


def test_busy_slot_queues_and_cancel_never_publishes(tmp_path, monkeypatch):
    import inspect
    blobs = MemoryBlobs()
    app = app_for(tmp_path, monkeypatch, blobs)
    submit_job = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/public/jobs" and "POST" in r.methods)
    gate = inspect.getclosurevars(submit_job).nonlocals["gate"]
    gate.acquire()
    try:
        with TestClient(app) as client:
            headers = session(client)
            receipt = upload(client, headers).json()["id"]
            assert client.get("/api/public/source-saves/" + receipt, headers=headers).json()["state"] == "queued"
            assert client.delete("/api/public/source-saves/" + receipt, headers=headers).status_code == 200
            assert wait(client, receipt, headers)["state"] == "cancelled"
    finally:
        gate.release()
    assert not json.loads(blobs.values["catalog-v1.json"][0])["papers"]


def test_cancel_during_validation_cannot_turn_into_saved(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def blocked(folder, uid):
        parse(folder, uid)
        entered.set()
        assert release.wait(5)
    blobs = MemoryBlobs()
    with TestClient(app_for(tmp_path, monkeypatch, blobs, blocked)) as client:
        headers = session(client)
        receipt = upload(client, headers).json()["id"]
        assert entered.wait(5)
        assert client.delete("/api/public/source-saves/" + receipt, headers=headers).status_code == 200
        release.set()
        assert wait(client, receipt, headers)["state"] == "cancelled"
        time.sleep(.05)
        assert not json.loads(blobs.values["catalog-v1.json"][0])["papers"]


@pytest.mark.parametrize("data,authorization,status", [(b"not a pdf", "true", 422), (SOURCE, "", 422), (b"%PDF-" + b"x" * (8 * 1024 * 1024), "true", 413)])
def test_invalid_or_unapproved_never_saved(tmp_path, monkeypatch, data, authorization, status):
    blobs = MemoryBlobs()
    with TestClient(app_for(tmp_path, monkeypatch, blobs)) as client:
        assert upload(client, session(client), data=data, authorization=authorization).status_code == status
    assert not blobs.values


def test_parse_failure_has_receipt_and_no_ready_entry(tmp_path, monkeypatch):
    blobs = MemoryBlobs()
    with TestClient(app_for(tmp_path, monkeypatch, blobs)) as client:
        headers = session(client)
        response = upload(client, headers, b"%PDF-1.7\nNot a complete document")
        assert response.status_code == 202
        result = wait(client, response.json()["id"], headers)
        assert result["state"] == "failed" and result["reason"]
    assert not json.loads(blobs.values["catalog-v1.json"][0])["papers"]
