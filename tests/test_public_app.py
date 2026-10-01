import hashlib
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from buna.public_app import create_public_app
from buna.public_corpus import load_corpus


def corpus(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    content = b"Synthetic licensed fixture only.\nOriginal public sample research text."
    digest = hashlib.sha256(content).hexdigest()
    (root / (digest + ".txt")).write_bytes(content)
    paper = {"sha256": digest, "filename": digest + ".txt", "title": "Synthetic sample",
             "license": "CC-BY-4.0", "license_url": "https://creativecommons.org/licenses/by/4.0/",
             "source_url": "https://example.org/synthetic", "version": "Synthetic fixture",
             "attribution": "Synthetic author, original test fixture.", "license_evidence": "Test fixture explicitly licensed.",
             "redistribution_approved": True}
    manifest = {"schema_version": 1, "purpose": "public-redistribution", "papers": [paper]}
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root, paper


def worker(folder, uid):
    (folder / "report.pdf").write_bytes(b"%PDF-1.7\nsynthetic fixture")
    (folder / "report.json").write_text('{"synthetic":true}')
    (folder / "complete.json").write_text('{"checked":1,"total":1,"overlap_percent":0}')


def test_public_corpus_fails_closed(tmp_path):
    root, paper = corpus(tmp_path)
    assert load_corpus(root)[0]["sha256"] == paper["sha256"]
    path = root / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["papers"][0]["license"] = "CC-BY-NC-4.0"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="license"):
        load_corpus(root)
    manifest["papers"][0]["license"] = "CC-BY-4.0"
    manifest["papers"][0]["filename"] = "../private.pdf"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load_corpus(root)


def test_cross_visitor_isolation_admin_denial_and_private_download(tmp_path):
    root, paper = corpus(tmp_path)
    app = create_public_app(root, tmp_path, worker_runner=worker)
    with TestClient(app) as c:
        a = {"Authorization": "Bearer " + c.post("/api/public/session").json()["token"]}
        b = {"Authorization": "Bearer " + c.post("/api/public/session").json()["token"]}
        for path in ["/api/jobs", "/api/library/papers", "/api/library/imports", "/api/library/collections/default"]:
            assert c.get(path).status_code == 404
            assert c.post(path, json={}).status_code == 404
        assert c.post("/api/public/jobs", files={"target": ("a.txt", b"words")}).status_code == 401
        response = c.post("/api/public/jobs", headers=a, data={"selected": json.dumps([paper["sha256"]])},
                          files={"target": ("a.txt", b"original synthetic manuscript words")})
        assert response.status_code == 202, response.text
        path = "/api/public/jobs/" + response.json()["id"]
        for _ in range(100):
            if c.get(path, headers=a).json()["status"] == "complete":
                break
            time.sleep(.01)
        assert c.get(path, headers=b).status_code == 404
        assert c.get(path + "/report.pdf", headers=b).status_code == 404
        assert c.delete(path, headers=b).status_code == 404
        assert c.get(path + "/report.pdf").status_code == 401
        assert c.get(path + "/report.pdf", headers=a).content.startswith(b"%PDF-")
        assert c.delete(path, headers=a).json()["status"] == "deleted"
        assert c.get(path, headers=a).status_code == 404


def test_public_origin_quota_and_allowlist(tmp_path, monkeypatch):
    root, paper = corpus(tmp_path)
    monkeypatch.setenv("BUNA_PUBLIC_ORIGIN", "https://example.github.io")
    with TestClient(create_public_app(root, tmp_path, worker_runner=worker)) as c:
        assert c.post("/api/public/session", headers={"Origin": "https://evil.example"}).status_code == 403
        response = c.post("/api/public/session", headers={"Origin": "https://example.github.io"})
        assert response.headers["access-control-allow-origin"] == "https://example.github.io"
        owner = {"Authorization": "Bearer " + response.json()["token"]}
        for selected in [["not-approved"], [["malformed"]], [paper["sha256"], paper["sha256"]]]:
            result = c.post("/api/public/jobs", headers=owner, data={"selected": json.dumps(selected)},
                            files={"target": ("target.txt", b"synthetic")})
            assert result.status_code == 422
        assert c.post("/api/public/jobs", headers={**owner, "Content-Length": str(41 * 1024 * 1024 + 1)}).status_code == 413
        import buna.public_app as module
        monkeypatch.setattr(module, "MAX_JOBS", 0)
        result = c.post("/api/public/jobs", headers=owner, files={"target": ("target.txt", b"synthetic")})
        assert result.status_code == 429


def test_public_requires_linux_container_without_test_runner(tmp_path):
    root, _ = corpus(tmp_path)
    import sys
    if sys.platform != "linux":
        with pytest.raises(RuntimeError, match="Linux"):
            create_public_app(root, tmp_path)


def test_worker_symlink_artifact_fails_closed(tmp_path):
    root, paper = corpus(tmp_path)
    secret = tmp_path / "private-sentinel.txt"
    secret.write_text("private sentinel must never be served")
    def malicious_worker(folder, uid):
        worker(folder, uid)
        (folder / "report.pdf").unlink()
        (folder / "report.pdf").symlink_to(secret)
    with TestClient(create_public_app(root, tmp_path, worker_runner=malicious_worker)) as c:
        owner = {"Authorization": "Bearer " + c.post("/api/public/session").json()["token"]}
        response = c.post("/api/public/jobs", headers=owner, data={"selected": json.dumps([paper["sha256"]])},
                          files={"target": ("test.txt", b"synthetic target")})
        path = "/api/public/jobs/" + response.json()["id"]
        for _ in range(100):
            result = c.get(path, headers=owner).json()
            if result["status"] != "running":
                break
            time.sleep(.01)
        assert result["status"] == "failed"
        assert c.get(path + "/report.pdf", headers=owner).status_code == 409
        assert secret.read_text() == "private sentinel must never be served"


def test_expired_results_removed_and_uids_never_reused(tmp_path, monkeypatch):
    root, paper = corpus(tmp_path)
    uids = []
    def record_worker(folder, uid):
        uids.append(uid)
        worker(folder, uid)
    with TestClient(create_public_app(root, tmp_path, worker_runner=record_worker)) as c:
        owner = {"Authorization": "Bearer " + c.post("/api/public/session").json()["token"]}
        def submit():
            result = c.post("/api/public/jobs", headers=owner,
                            data={"selected": json.dumps([paper["sha256"]])},
                            files={"target": ("test.txt", b"synthetic target")})
            assert result.status_code == 202
            path = "/api/public/jobs/" + result.json()["id"]
            for _ in range(100):
                if c.get(path, headers=owner).json()["status"] != "running":
                    break
                time.sleep(.01)
            return path
        first = submit()
        c.delete(first, headers=owner).raise_for_status()
        second = submit()
        assert len(uids) == 2 and uids[1] > uids[0]
        import buna.public_app as module
        monkeypatch.setattr(module, "RETENTION", 0)
        assert c.get(second, headers=owner).status_code in {401, 404}
        c.post("/api/public/session").raise_for_status()
        runtime = next(tmp_path.glob("public-runtime-*"))
        assert not (runtime / second.rsplit("/", 1)[-1]).exists()


def test_comparison_model_is_explicit_and_standard_by_default(tmp_path):
    root, paper = corpus(tmp_path)
    models = []
    def record_worker(folder, uid):
        models.append(json.loads((folder / "request.json").read_text())["comparison_model"])
        worker(folder, uid)
    with TestClient(create_public_app(root, tmp_path, worker_runner=record_worker)) as client:
        owner = {"Authorization": "Bearer " + client.post("/api/public/session").json()["token"]}
        for model in [None, "classified-v1.1", "unknown-model", "improvedEng"]:
            fields = {"selected": json.dumps([paper["sha256"]])}
            if model is not None:
                fields["comparison_model"] = model
            response = client.post("/api/public/jobs", headers=owner, data=fields,
                                   files={"target": ("synthetic.txt", b"original synthetic test")})
            if model == "unknown-model":
                assert response.status_code == 422
                continue
            assert response.status_code == 202, response.text
            path = "/api/public/jobs/" + response.json()["id"]
            for _ in range(100):
                if client.get(path, headers=owner).json()["status"] != "running":
                    break
                time.sleep(.01)
        assert models == ["validated-lexical", "classified-v1.1", "improvedEng"]


def test_similar_diagnostics_csv_uses_same_ownership_and_saved_evidence(tmp_path):
    root, paper = corpus(tmp_path)

    def diagnostic_worker(folder, uid):
        worker(folder, uid)
        summary = json.loads((folder / "complete.json").read_text())
        summary.update(algorithm_version="improvedEng-v3-precision",
                       eligibility_profile="improvedEng-layout-longquotes-v1",
                       word_accounting={"header_removed_words": 8})
        (folder / "complete.json").write_text(json.dumps(summary))
        (folder / "report.json").write_text(json.dumps({"improved_eng": {
            "similar_diagnostics": [{"source": "1", "anchor_length": 4, "matched_word_count": 10,
                                    "reason_match_terminated": ["local-density"]}]}}))

    with TestClient(create_public_app(root, tmp_path, worker_runner=diagnostic_worker)) as client:
        owner = {"Authorization": "Bearer " + client.post("/api/public/session").json()["token"]}
        other = {"Authorization": "Bearer " + client.post("/api/public/session").json()["token"]}
        response = client.post("/api/public/jobs", headers=owner,
                               data={"selected": json.dumps([paper["sha256"]]), "comparison_model": "improvedEng"},
                               files={"target": ("original.txt", b"Original synthetic manuscript.")})
        assert response.status_code == 202
        path = "/api/public/jobs/" + response.json()["id"]
        for _ in range(100):
            if client.get(path, headers=owner).json()["status"] == "complete":
                break
            time.sleep(.01)
        saved = client.get(path, headers=owner).json()
        assert saved["algorithm_version"] == "improvedEng-v3-precision"
        assert saved["eligibility_profile"] == "improvedEng-layout-longquotes-v1"
        assert saved["word_accounting"]["header_removed_words"] == 8
        csv = client.get(path + "/report.csv", headers=owner)
        assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
        assert "anchor_length" in csv.text and "local-density" in csv.text
        assert client.get(path + "/report.csv", headers=other).status_code == 404
        assert client.get(path + "/report.csv").status_code == 401
