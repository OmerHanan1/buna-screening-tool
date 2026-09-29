import hashlib
import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from buna import comparison
from buna.documents import _structure
from buna.public_app import create_public_app
from buna.hosted_runtime import safe_progress
from test_public_app import corpus


def test_total_budget_retains_completed_evidence_without_rescoring(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(comparison.time, "monotonic", lambda: now[0])
    phrase = "amber birds gather beside quiet rivers during winter mornings"
    def document(text):
        return _structure([{"number": 1, "text": text}], "Synthetic", [])
    target = document("Abstract\nOriginal synthetic observations describe " + phrase + ".")
    sources = [{"id": "a", "document": document("Source A. " + phrase)},
               {"id": "b", "document": document("Source B. " + phrase)}]
    def completed(row, matches):
        if row["source_id"] == "a":
            now[0] = 3.0
    result = comparison.compare_documents(target, sources, source_done=completed, total_time_limit_seconds=2)
    assert result["source_coverage"][0]["status"] == "compared"
    assert result["source_coverage"][1]["status"] == "skipped-time-limit"
    assert result["metrics"]["truncated"]
    assert result["metrics"]["overlapping_words"] == 9
    assert result["matches"] and all(m["source_id"] == "a" for m in result["matches"])


def test_diagnostic_reader_rejects_content_and_symlinks(tmp_path):
    (tmp_path / "progress.json").write_text(json.dumps({
        "stage": "compare", "source_index": 4, "source_count": 44,
        "private_text": "MUST NOT be exposed", "path": "/private/file.pdf",
        "checked_sources": -1,
    }))
    assert safe_progress(tmp_path) == {"stage": "compare", "source_index": 4, "source_count": 44}
    (tmp_path / "progress.json").unlink()
    (tmp_path / "progress.json").symlink_to(tmp_path / "private.json")
    assert safe_progress(tmp_path) == {}


def test_pdf_failure_keeps_completed_json_and_specific_error(tmp_path):
    root, paper = corpus(tmp_path)
    def worker(folder, uid):
        (folder / "progress.json").write_text(json.dumps({"stage": "render-pdf", "checked_sources": 1}))
        (folder / "comparison-complete.json").write_text(json.dumps({"checked": 1, "total": 1, "overlap_percent": 12.5}))
        (folder / "report.json").write_text('{"metrics":{"overlap_percent":12.5}}')
        raise RuntimeError("Synthetic PDF failure, not a manuscript error")
    with TestClient(create_public_app(root, tmp_path, worker_runner=worker)) as c:
        auth = {"Authorization": "Bearer " + c.post("/api/public/session").json()["token"]}
        response = c.post("/api/public/jobs", headers=auth, data={"selected": json.dumps([paper["sha256"]])},
                          files={"target": ("target.txt", b"synthetic target")})
        path = "/api/public/jobs/" + response.json()["id"]
        for _ in range(100):
            state = c.get(path, headers=auth).json()
            if state["status"] != "running":
                break
            time.sleep(.01)
        assert state["status"] == "report-failed"
        assert state["error_code"] == "pdf-error" and state["evidence_available"]
        assert state["checked"] == 1 and state["overlap_percent"] == 12.5
        assert c.get(path + "/report.json", headers=auth).status_code == 200
        assert c.get(path + "/report.pdf", headers=auth).status_code == 409
        assert "synthetic" not in state["error"].lower()


def test_worker_crash_has_reference_and_no_private_exception_text(tmp_path):
    root, paper = corpus(tmp_path)
    def worker(folder, uid):
        (folder / "progress.json").write_text('{"stage":"parse-manuscript"}')
        raise ValueError("/private/path SECRET DOCUMENT CONTENT")
    with TestClient(create_public_app(root, tmp_path, worker_runner=worker)) as c:
        auth = {"Authorization": "Bearer " + c.post("/api/public/session").json()["token"]}
        response = c.post("/api/public/jobs", headers=auth, data={"selected": json.dumps([paper["sha256"]])},
                          files={"target": ("target.txt", b"synthetic target")})
        path = "/api/public/jobs/" + response.json()["id"]
        for _ in range(100):
            state = c.get(path, headers=auth).json()
            if state["status"] != "running":
                break
            time.sleep(.01)
        assert state["status"] == "failed"
        assert state["diagnostic_id"] == response.json()["id"]
        assert state["progress"]["stage"] == "parse-manuscript"
        assert "SECRET" not in json.dumps(state) and "/private" not in json.dumps(state)


def test_cache_identity_and_hash_are_required(tmp_path):
    from buna.corpus_cache import validate_cache
    import pypdf
    digest = "a" * 64
    content = b'{"text":"synthetic","pages":[]}'
    path = tmp_path / (digest + ".json")
    path.write_bytes(content)
    paper = {"sha256": digest, "parsed_cache": {"filename": path.name, "sha256": hashlib.sha256(content).hexdigest(),
             "source_sha256": digest, "structure_version": 3, "profile": "source-v1",
             "parser": "pypdf", "parser_version": pypdf.__version__}}
    validate_cache(tmp_path, paper)
    path.write_bytes(b"modified")
    with pytest.raises(ValueError, match="fingerprint"):
        validate_cache(tmp_path, paper)


def test_submission_retry_reuses_job_without_cross_visitor_access(tmp_path):
    from test_public_app import worker
    root, paper = corpus(tmp_path)
    with TestClient(create_public_app(root, tmp_path, worker_runner=worker)) as c:
        first = {"Authorization": "Bearer " + c.post("/api/public/session").json()["token"],
                 "Idempotency-Key": str(uuid4())}
        second = {"Authorization": "Bearer " + c.post("/api/public/session").json()["token"],
                  "Idempotency-Key": first["Idempotency-Key"]}
        def submit(headers):
            response = c.post("/api/public/jobs", headers=headers, data={"selected": json.dumps([paper["sha256"]])},
                              files={"target": ("target.txt", b"synthetic target")})
            assert response.status_code == 202, response.text
            return response.json()["id"]
        job = submit(first)
        for _ in range(100):
            if c.get("/api/public/jobs/" + job, headers=first).json()["status"] != "running":
                break
            time.sleep(.01)
        assert submit(first) == job
        assert submit(second) != job
        assert c.get("/api/public/jobs/" + job, headers=second).status_code == 404


def test_preparsed_cache_preserves_original_offsets_and_matching(tmp_path):
    from buna.corpus_cache import build
    from buna.documents import extract_document
    source_root = tmp_path / "raw"
    source_root.mkdir()
    text = b"Source title\nAmber birds gather beside quiet rivers during winter mornings.\nOriginal source ending."
    digest = hashlib.sha256(text).hexdigest()
    source = source_root / (digest + ".txt")
    source.write_bytes(text)
    (source_root / "manifest.json").write_text(json.dumps({"papers": [{"sha256": digest, "filename": source.name}]}))
    destination = tmp_path / "cached"
    build(source_root, destination)
    direct = extract_document(source, profile="source")
    cached = json.loads((destination / (digest + ".json")).read_text())
    assert cached == direct
    assert all(cached["text"][s["start"]:s["end"]] == s["text"] for s in cached["segments"])
    target = _structure([{"number": 1, "text": "Abstract\nSynthetic observations show amber birds gather beside quiet rivers during winter mornings."}], "Target", [])
    first = comparison.compare_documents(target, [{"id": "a", "document": direct}])
    second = comparison.compare_documents(target, [{"id": "a"}], load_document=lambda _: cached)
    assert first["metrics"] == second["metrics"]
    assert first["matches"] == second["matches"]
