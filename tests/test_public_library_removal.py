import json
import threading
import time

from fastapi.testclient import TestClient

from buna.public_app import create_public_app
from test_public_app import corpus, worker
from test_shared_library import MemoryBlobs
from test_shared_library_api import setup, session


def test_bundled_removal_requires_capability_and_specific_global_confirmation(tmp_path, monkeypatch):
    setup(monkeypatch)
    root, paper = corpus(tmp_path)
    app = create_public_app(root, tmp_path, worker_runner=worker, shared_store=MemoryBlobs())
    path = "/api/public/library/" + paper["sha256"]
    body = {"confirm_sha256": paper["sha256"], "expected_version": "0", "affects_everyone": True}
    with TestClient(app) as client:
        assert client.request("DELETE", path, json=body).status_code == 401
        assert client.request("DELETE", path, json=body, headers={"Authorization": "Bearer invalid"}).status_code == 401
        headers = session(client)
        assert client.request("DELETE", path, json={**body, "affects_everyone": False}, headers=headers).status_code == 422
        assert client.request("DELETE", path, json=body, headers={**headers, "Origin": "https://untrusted.invalid"}).status_code == 403
        response = client.request("DELETE", path, json=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["packaged_bytes_retained"] is True
        assert response.json()["scope"] == "all-users"
        assert app.state.shared_library.visible([paper]) == []
        assert client.get("/api/public/library", headers=headers).json()["papers"] == []
        other = session(client)
        assert client.request("DELETE", path, json=body, headers=other).json()["already_removed"] is True
        assert client.request("DELETE", "/api/public/library/" + "f" * 64,
                              json={**body, "confirm_sha256": "f" * 64}, headers=headers).status_code == 404


def test_running_snapshot_and_report_survive_global_removal(tmp_path, monkeypatch):
    setup(monkeypatch)
    root, paper = corpus(tmp_path)
    entered, release = threading.Event(), threading.Event()
    def paused(folder, uid):
        request = json.loads((folder / "request.json").read_text())
        source = folder / request["sources"][0]["path"]
        before = source.read_bytes()
        entered.set()
        assert release.wait(5)
        assert source.read_bytes() == before
        worker(folder, uid)
    with TestClient(create_public_app(root, tmp_path, worker_runner=paused, shared_store=MemoryBlobs())) as client:
        headers = session(client)
        data = {"selected": json.dumps([paper["sha256"]])}
        response = client.post("/api/public/jobs", headers=headers, data=data,
                               files={"target": ("target.txt", b"Original synthetic manuscript text.")})
        assert response.status_code == 202
        job = response.json()["id"]
        assert entered.wait(5)
        try:
            removed = client.request("DELETE", "/api/public/library/" + paper["sha256"], headers=headers,
                                     json={"confirm_sha256": paper["sha256"], "expected_version": "0", "affects_everyone": True})
            assert removed.status_code == 200
            assert client.get("/api/public/library", headers=session(client)).json()["papers"] == []
        finally:
            release.set()
        for _ in range(100):
            if client.get("/api/public/jobs/" + job, headers=headers).json()["status"] == "complete":
                break
            time.sleep(.01)
        assert client.get("/api/public/jobs/" + job + "/report.pdf", headers=headers).content.startswith(b"%PDF-")
        time.sleep(.05)
        stale = client.post("/api/public/jobs", headers=headers, data=data,
                            files={"target": ("new.txt", b"Original second synthetic manuscript.")})
        assert stale.status_code == 409
        assert "Refresh" in stale.json()["detail"]


def test_catalog_outage_does_not_resurrect_bundled_defaults(tmp_path, monkeypatch):
    setup(monkeypatch)
    root, paper = corpus(tmp_path)
    store = MemoryBlobs()
    with TestClient(create_public_app(root, tmp_path, worker_runner=worker, shared_store=store)) as client:
        headers = session(client)
        store.read = lambda *_: (_ for _ in ()).throw(OSError("Synthetic outage"))
        result = client.get("/api/public/library", headers=headers).json()
        assert result["papers"] == [] and result["shared_library_warning"]
        assert not result["library_removal_available"] and not result["shared_saving_available"]
        response = client.post("/api/public/jobs", headers=headers,
                               data={"selected": json.dumps([paper["sha256"]])},
                               files={"target": ("target.txt", b"Original synthetic manuscript.")})
        assert response.status_code == 503
