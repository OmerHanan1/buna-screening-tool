"""Offline synthetic 44+50 verification; run in the existing 1 CPU / 2 GiB image."""
import hashlib
import json
import os
from pathlib import Path
import random
import tempfile
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pymupdf
import pypdf

from buna.documents import extract_document, STRUCTURE_VERSION
from buna.public_app import create_public_app


def original_pdf(seed, pages=8):
    rng = random.Random(seed)
    words = ("observations participants instruments evaluation research samples methods "
             "measurements laboratory conditions responses controlled preliminary analysis "
             "results conclusions independent variance estimates regional factors temporal "
             "processes experimental framework hypothesis distribution evidence comparison "
             "replication procedure accuracy recorded effects values assessment changes "
             "population attention cognition temperature stability humidity calibration").split()
    with pymupdf.open() as document:
        for number in range(pages):
            page = document.new_page()
            text = f"Original synthetic study {seed}, page {number + 1}\nIntroduction\n"
            text += "\n\n".join(" ".join(rng.choice(words) for _ in range(65)) + "." for _ in range(4))
            if number == 0:
                text += "\nThe ceramic sensor records a stable sequence of local measurements during carefully controlled laboratory cycles."
            assert page.insert_textbox(pymupdf.Rect(40, 40, 555, 800), text, fontsize=9) >= 0
        return document.tobytes()


def main():
    os.environ.update(BUNA_ACCESS_MODE="email-gate", BUNA_ALLOWED_EMAIL="fixture@example.org",
                      BUNA_PUBLIC_ORIGIN="https://fixture.invalid", BUNA_PUBLIC_HOSTS="testserver")
    for key in ("BUNA_TEAM_CORPUS_SHA", "BUNA_SHARED_ACCOUNT_URL", "BUNA_SHARED_CONTAINER", "BUNA_SHARED_IDENTITY_CLIENT_ID"):
        os.environ.pop(key, None)
    with tempfile.TemporaryDirectory(prefix="bulk-synthetic-") as temporary:
        root = Path(temporary)
        root.chmod(0o755)
        corpus = root / "corpus"
        corpus.mkdir()
        papers = []
        for number in range(44):
            raw = original_pdf(number)
            digest = hashlib.sha256(raw).hexdigest()
            path = corpus / (digest + ".pdf")
            path.write_bytes(raw)
            parsed = json.dumps(extract_document(path, profile="source")).encode()
            (corpus / (digest + ".json")).write_bytes(parsed)
            papers.append({"sha256": digest, "filename": path.name, "title": f"Synthetic curated {number}",
                           "hosted_processing_basis": "user-attested-hosted-use", "attribution": "Original generated synthetic fixture.",
                           "license": "CC0-1.0", "license_url": "https://example.org/synthetic-license",
                           "source_url": "https://example.org/synthetic", "version": "Synthetic original eight-page PDF",
                           "parsed_cache": {"filename": digest + ".json", "sha256": hashlib.sha256(parsed).hexdigest(),
                                            "source_sha256": digest, "structure_version": STRUCTURE_VERSION,
                                            "profile": "source-v1", "parser": "pypdf", "parser_version": pypdf.__version__}})
        manifest = {"schema_version": 1, "purpose": "private-hosted-processing",
                    "access_policy": "email-gate-with-independent-visitor-capabilities",
                    "user_attestation": {"confirmed": True, "scope": "hosted-storage-processing-and-matching-excerpts",
                                         "recorded_at": "2026-09-30", "statement": "Original synthetic test fixture only."},
                    "papers": papers}
        raw_manifest = json.dumps(manifest).encode()
        (corpus / "manifest.json").write_bytes(raw_manifest)
        os.environ["BUNA_ATTESTED_CORPUS_SHA"] = hashlib.sha256(raw_manifest).hexdigest()
        app = create_public_app(corpus, root)
        with TestClient(app) as client:
            token = client.post("/api/public/session", json={"email": "fixture@example.org"}).json()["token"]
            headers = {"Authorization": "Bearer " + token}
            references, total_bytes = [], 0
            for number in range(50):
                source = original_pdf(100 + number)
                total_bytes += len(source)
                response = client.post("/api/public/source-uploads",
                                       headers={**headers, "Idempotency-Key": str(uuid4())},
                                       files={"source": (f"Original synthetic source {number}.pdf", source)})
                response.raise_for_status()
                references.append(response.json()["id"])
            started = time.monotonic()
            response = client.post("/api/public/jobs", headers=headers,
                                   data={"selected": json.dumps([p["sha256"] for p in papers]),
                                         "uploaded_sources": json.dumps(references)},
                                   files={"target": ("Synthetic manuscript.pdf", original_pdf(1000, pages=45))})
            response.raise_for_status()
            assert response.json()["source_count"] == 94, response.text
            path = "/api/public/jobs/" + response.json()["id"]
            while time.monotonic() - started < 850:
                time.sleep(3)
                response = client.get(path, headers=headers)
                if response.status_code == 429:
                    time.sleep(60)
                    continue
                response.raise_for_status()
                state = response.json()
                if state["status"] != "running":
                    break
            assert state["status"] in {"complete", "report-failed"}, state
            assert state["total"] == 94, state
            report = client.get(path + "/report.json", headers=headers)
            report.raise_for_status()
            assert len(report.json()["source_coverage"]) == 94
            report_pdf = client.get(path + "/report.pdf", headers=headers)
            report_pdf.raise_for_status()
            assert report_pdf.content.startswith(b"%PDF-")
            runtime = next(root.glob("public-runtime-*"))
            import sqlite3
            with sqlite3.connect(runtime / "index.sqlite3") as db:
                diagnostic = json.loads(db.execute("SELECT data FROM diagnostics WHERE job=?", (state["id"],)).fetchone()[0])
            print(json.dumps({"sources": 94, "manual_uploads": 50, "source_pages_each": 8, "manuscript_pages": 45,
                              "checked": state["checked"], "partial": state["partial"], "pdf_bytes": len(report_pdf.content),
                              "upload_bytes": total_bytes, "wall_seconds": round(time.monotonic() - started, 2),
                              "peak_rss_bytes": diagnostic.get("peak_rss_bytes"), "cpu_seconds": diagnostic.get("cpu_seconds")},
                             indent=2), flush=True)


if __name__ == "__main__":
    main()
