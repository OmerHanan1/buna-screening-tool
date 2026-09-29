import hashlib
import json
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from buna.public_app import create_public_app
from test_public_app import corpus, worker


@pytest.fixture
def gated(tmp_path, monkeypatch, request):
    root, paper = corpus(tmp_path)
    monkeypatch.setenv("BUNA_ACCESS_MODE", "email-gate")
    monkeypatch.setenv("BUNA_ALLOWED_EMAIL", getattr(request, "param", " Owner@Example.org "))
    monkeypatch.setenv("BUNA_PUBLIC_ORIGIN", "https://example.github.io")
    with TestClient(create_public_app(root, tmp_path, worker_runner=worker)) as client:
        yield client, paper, tmp_path


def enter(client, email="owner@example.org"):
    response = client.post("/api/public/session", json={"email": email})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["token"]}


@pytest.mark.parametrize("gated", [
    " Owner@Example.org ",
    " Owner@Example.org , Second@Example.org ",
    " Owner@Example.org , OWNER@EXAMPLE.ORG ",
], indirect=True)
def test_email_gate_normalizes_but_does_not_verify_identity(gated):
    client, paper, root = gated
    assert client.get("/health").json()["mode"] == "email-gate"
    config = client.get("/api/auth/config").json()
    assert config == {"mode": "email-gate", "identity_verified": False}
    assert client.get("/api/public/library").status_code == 401
    assert client.post("/api/public/session", json={"email": "wrong@example.org"}).status_code == 403
    assert client.post("/api/public/session", json={"email": "invalid"}).status_code == 403
    first = enter(client, "  OWNER@EXAMPLE.ORG  ")
    second = enter(client)
    assert first != second
    assert client.get("/api/public/library", headers=first).status_code == 200
    assert client.get("/api/public/library", headers=second).status_code == 200
    database = next(root.glob("public-runtime-*/index.sqlite3"))
    with sqlite3.connect(database) as db:
        rows = db.execute("SELECT token FROM sessions").fetchall()
        assert len(rows) == 2
        assert all("owner" not in row[0] and len(row[0]) == 64 for row in rows)


@pytest.mark.parametrize("gated,second_email", [
    (" Owner@Example.org ", "owner@example.org"),
    (" Owner@Example.org , Second@Example.org ", "  SECOND@EXAMPLE.ORG  "),
], indirect=["gated"])
def test_email_sessions_cannot_inherit_jobs(gated, second_email):
    client, paper, root = gated
    first, second = enter(client), enter(client, second_email)
    assert first != second
    response = client.post("/api/public/jobs", headers=first,
                          data={"selected": json.dumps([paper["sha256"]])},
                          files={"target": ("target.txt", b"synthetic original target")})
    assert response.status_code == 202
    path = "/api/public/jobs/" + response.json()["id"]
    for _ in range(100):
        if client.get(path, headers=first).json()["status"] != "running":
            break
        time.sleep(.01)
    for suffix in ["", "/report.pdf", "/report.json"]:
        assert client.get(path + suffix, headers=second).status_code == 404
        assert client.get(path + suffix).status_code == 401
    assert client.delete(path, headers=second).status_code == 404
    assert client.get(path + "/report.pdf", headers=first).content.startswith(b"%PDF-")
    assert client.delete(path, headers=first).json()["status"] == "deleted"
    assert client.get(path, headers=first).status_code == 404


@pytest.mark.parametrize("gated", [
    " Owner@Example.org , Second@Example.org ",
    " Owner@Example.org , Second@Example.org , SECOND@EXAMPLE.ORG ",
], indirect=True)
def test_multiple_emails_preserve_gate_and_empty_independent_sessions(gated):
    client, paper, root = gated
    first = enter(client, "  OWNER@EXAMPLE.ORG ")
    second = enter(client, " SECOND@EXAMPLE.ORG  ")
    assert first != second
    assert client.get("/api/auth/config").json() == {"mode": "email-gate", "identity_verified": False}
    assert client.get("/api/public/library", headers=first).status_code == 200
    assert client.get("/api/public/library", headers=second).status_code == 200
    assert client.get("/api/public/library").status_code == 401
    for email in ["wrong@example.org", "owner@example.org.evil", "owner@example.org,second@example.org"]:
        assert client.post("/api/public/session", json={"email": email}).status_code == 403
    with sqlite3.connect(next(root.glob("public-runtime-*/index.sqlite3"))) as db:
        rows = db.execute("SELECT token FROM sessions").fetchall()
        assert len(rows) == 2
        assert all(len(row[0]) == 64 and "@" not in row[0] for row in rows)
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize("setting", [
    "", " ", ",", "owner@example.org,", ",owner@example.org",
    "owner@example.org,,second@example.org", "owner@example.org,invalid",
    "owner@example.org\nsecond@example.org",
    "owner@example.org," + "x" * 65 + "@example.org",
    "owner@example.org," + "x@" + "y" * 249 + ".org",
])
def test_invalid_allowlist_fails_closed(tmp_path, monkeypatch, setting):
    root, _ = corpus(tmp_path)
    monkeypatch.setenv("BUNA_ACCESS_MODE", "email-gate")
    monkeypatch.setenv("BUNA_ALLOWED_EMAIL", setting)
    with pytest.raises(RuntimeError, match="email allowlist entries"):
        create_public_app(root, tmp_path, worker_runner=worker)


def test_expired_capability_rate_limits_and_admin_denials(gated):
    client, paper, root = gated
    headers = enter(client)
    with sqlite3.connect(next(root.glob("public-runtime-*/index.sqlite3"))) as db:
        db.execute("UPDATE sessions SET created=?", (time.time() - 24 * 3600,))
    assert client.get("/api/public/library", headers=headers).status_code == 401
    headers = enter(client)
    for path in ["/api/jobs", "/api/library/imports", "/api/library/papers",
                 "/api/public/sources/" + paper["sha256"]]:
        assert client.get(path, headers=headers).status_code == 404
        assert client.post(path, headers=headers, json={}).status_code == 404
    assert client.get("/api/public/library", headers={"Authorization": "Bearer " + "x" * 200}).status_code == 401
    responses = [client.post("/api/public/session", json={"email": "wrong@example.org"}) for _ in range(11)]
    assert responses[-1].status_code == 429


def test_email_gate_does_not_enable_identity_bound_private_corpus(tmp_path, monkeypatch):
    root, _ = corpus(tmp_path)
    monkeypatch.setenv("BUNA_ACCESS_MODE", "email-gate")
    monkeypatch.setenv("BUNA_ALLOWED_EMAIL", "owner@example.org")
    monkeypatch.setenv("BUNA_TEAM_CORPUS_SHA", "0" * 64)
    with pytest.raises(RuntimeError, match="requires complete team"):
        create_public_app(root, tmp_path, worker_runner=worker)


def test_attested_manifest_is_explicit_not_a_cc_claim(tmp_path):
    from buna.team_corpus import load_attested_corpus
    root, paper = corpus(tmp_path)
    paper["hosted_processing_basis"] = "user-attested-hosted-use"
    manifest = {"schema_version": 1, "purpose": "private-hosted-processing",
                "access_policy": "email-gate-with-independent-visitor-capabilities",
                "user_attestation": {"confirmed": True, "scope": "hosted-storage-processing-and-matching-excerpts",
                                     "recorded_at": "2026-09-29", "statement": "Synthetic authorization"},
                "papers": [paper]}
    def save():
        raw = json.dumps(manifest).encode()
        (root / "manifest.json").write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()
    assert len(load_attested_corpus(root, save())) == 1
    paper["hosted_processing_basis"] = "downloaded-only"
    with pytest.raises(ValueError, match="attestation"):
        load_attested_corpus(root, save())
