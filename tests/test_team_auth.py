import json
import time
from types import SimpleNamespace
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
import jwt
import pytest

from buna.team_auth import TeamConfig, TeamAuthenticator
from buna.public_app import create_public_app
from test_public_app import corpus, worker


@pytest.fixture
def auth():
    config = TeamConfig(str(uuid4()), str(uuid4()), str(uuid4()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    class Keys:
        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=key.public_key())
    return TeamAuthenticator(config, Keys()), key


def token(auth, **changes):
    service, key = auth
    claims = {"iss": service.config.issuer, "aud": service.config.client,
              "tid": service.config.tenant, "oid": service.config.owner_oid,
              "sub": "stable-test-subject", "iat": int(time.time()), "nbf": int(time.time()) - 1,
              "exp": int(time.time()) + 600, "ver": "2.0", "azp": service.config.client,
              "scp": "access_as_user"}
    claims.update(changes)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test-key"})


def test_owner_signature_identity_and_scope_required(auth):
    service, _ = auth
    owner = service.authenticate("Bearer " + token(auth))
    assert owner == service.authenticate("Bearer " + token(auth, sub="same-owner-new-session"))
    bad = [
        {"iss": "https://evil.example"}, {"aud": str(uuid4())}, {"tid": str(uuid4())},
        {"exp": int(time.time()) - 120}, {"nbf": int(time.time()) + 600},
        {"scp": ""}, {"azp": str(uuid4())}, {"ver": "1.0"},
    ]
    for change in bad:
        with pytest.raises(HTTPException) as error:
            service.authenticate("Bearer " + token(auth, **change))
        assert error.value.status_code == 401
    with pytest.raises(HTTPException) as denied:
        service.authenticate("Bearer " + token(auth, oid=str(uuid4()), email="omer.hanan@gmail.com"))
    assert denied.value.status_code == 403
    with pytest.raises(HTTPException):
        service.authenticate("Bearer " + jwt.encode({"email": "omer.hanan@gmail.com"}, "x" * 32, algorithm="HS256", headers={"kid": "test-key"}))


def test_team_every_api_requires_identity_and_jobs_stay_owned(auth, tmp_path, monkeypatch):
    root, paper = corpus(tmp_path)
    service, _ = auth
    monkeypatch.setenv("BUNA_PUBLIC_ORIGIN", "https://example.github.io")
    app = create_public_app(root, tmp_path, worker_runner=worker, team_authenticator=service)
    owner = {"Authorization": "Bearer " + token(auth)}
    outsider = {"Authorization": "Bearer " + token(auth, oid=str(uuid4()))}
    with TestClient(app) as client:
        assert client.get("/health").json()["mode"] == "team-restricted"
        assert client.get("/api/auth/config").json()["client_id"] == service.config.client
        for path in ["/api/public/library", "/api/public/jobs/missing", "/api/public/jobs/missing/report.pdf",
                     "/api/public/jobs/missing/report.json", "/api/jobs", "/api/library/papers"]:
            assert client.get(path).status_code == 401
            assert client.get(path, headers=outsider).status_code == 403
        denied = client.get("/api/public/library", headers={"Origin": "https://example.github.io"})
        assert denied.status_code == 401 and denied.headers["access-control-allow-origin"] == "https://example.github.io"
        assert client.post("/api/public/session").status_code == 401
        assert client.post("/api/public/session", headers=owner).status_code == 405
        assert client.get("/api/public/library", headers=owner).status_code == 200
        result = client.post("/api/public/jobs", headers=owner, data={"selected": json.dumps([paper["sha256"]])},
                             files={"target": ("synthetic.txt", b"synthetic target")})
        assert result.status_code == 202, result.text
        path = "/api/public/jobs/" + result.json()["id"]
        for _ in range(100):
            if client.get(path, headers=owner).json()["status"] != "running":
                break
            time.sleep(.01)
        assert client.get(path, headers=owner).status_code == 200
        assert client.get(path + "/report.pdf", headers=outsider).status_code == 403
        assert client.get(path + "/report.pdf", headers=owner).content.startswith(b"%PDF-")
        assert client.delete(path, headers=outsider).status_code == 403
        assert client.get("/api/public/sources/" + paper["sha256"], headers=owner).status_code == 404
        assert client.get("/api/jobs", headers=owner).status_code == 404


def test_partial_config_never_falls_back_to_anonymous(monkeypatch):
    monkeypatch.setenv("BUNA_TEAM_TENANT", str(uuid4()))
    monkeypatch.delenv("BUNA_TEAM_CLIENT", raising=False)
    monkeypatch.delenv("BUNA_TEAM_OWNER_OID", raising=False)
    with pytest.raises(RuntimeError, match="Incomplete"):
        TeamConfig.from_environment()


def test_report_export_keeps_entire_sequence_and_metrics():
    from buna.public_worker import source_excerpts_only
    sequence = "Original matched words with internal nonmatching text. " * 100
    before, after = "Unrelated preceding context. ", " Unrelated following context."
    report = {"metrics": {"overlap_percent": 23.45, "overlapping_words": 987},
              "matches": [{"source_id": "1", "aligned_pairs": [[3, 7]], "source": {
                  "text": before + sequence + after, "start": 100,
                  "end": 100 + len(before + sequence + after),
                  "match_start": 100 + len(before), "match_end": 100 + len(before + sequence),
                  "highlights": [[len(before), len(before) + 8]],
              }}], "warnings": []}
    source_excerpts_only(report)
    assert report["matches"][0]["source"]["text"] == sequence
    assert len(sequence) > 3000
    assert report["matches"][0]["source"]["highlights"] == [[0, 8]]
    assert report["matches"][0]["aligned_pairs"] == [[3, 7]]
    assert report["metrics"] == {"overlap_percent": 23.45, "overlapping_words": 987}
    assert len(report["matches"]) == 1


def test_private_corpus_requires_file_permission_and_pinned_identity(tmp_path):
    import hashlib
    from buna.team_corpus import load_team_corpus
    root, paper = corpus(tmp_path)
    tenant, owner = str(uuid4()), str(uuid4())
    manifest = {"schema_version": 1, "purpose": "private-team-processing",
                "authorized_identity": {"tenant": tenant, "object_id": owner}, "papers": [paper]}
    def write():
        raw = json.dumps(manifest).encode()
        (root / "manifest.json").write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError, match="permission"):
        load_team_corpus(root, write(), tenant, owner)
    paper["hosted_processing_permission"] = {
        "approved": True, "scope": "owner-hosted-research-reports-only",
        "basis": "Synthetic fixture license", "evidence": "Original synthetic grant",
        "reviewer": "Synthetic test reviewer", "reviewed_at": "2026-09-29",
    }
    fingerprint = write()
    assert len(load_team_corpus(root, fingerprint, tenant, owner)) == 1
    with pytest.raises(ValueError, match="identity"):
        load_team_corpus(root, fingerprint, tenant, str(uuid4()))
    with pytest.raises(ValueError, match="fingerprint"):
        load_team_corpus(root, "0" * 64, tenant, owner)


def test_private_corpus_cannot_start_anonymously(tmp_path, monkeypatch):
    root, _ = corpus(tmp_path)
    monkeypatch.setenv("BUNA_TEAM_CORPUS_SHA", "0" * 64)
    for name in ("BUNA_TEAM_TENANT", "BUNA_TEAM_CLIENT", "BUNA_TEAM_OWNER_OID"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="requires complete team"):
        create_public_app(root, tmp_path, worker_runner=worker)
