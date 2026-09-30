import inspect

from fastapi.testclient import TestClient

from buna.public_app import create_public_app
from buna.public_library_removal import install_library_removal
from test_public_app import corpus, worker
from test_shared_library import MemoryBlobs
from test_shared_library_api import setup, session


def test_bundled_removal_requires_capability_and_specific_global_confirmation(tmp_path, monkeypatch):
    setup(monkeypatch)
    root, paper = corpus(tmp_path)
    app = create_public_app(root, tmp_path, worker_runner=worker, shared_store=MemoryBlobs())
    session_route = next(r.endpoint for r in app.routes if getattr(r, "path", "") == "/api/public/jobs" and "POST" in r.methods)
    check_session = inspect.getclosurevars(session_route).nonlocals["session"]
    install_library_removal(app, shared=app.state.shared_library, session=check_session)
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
        other = session(client)
        assert client.request("DELETE", path, json=body, headers=other).json()["already_removed"] is True
        assert client.request("DELETE", "/api/public/library/" + "f" * 64,
                              json={**body, "confirm_sha256": "f" * 64}, headers=headers).status_code == 404
