"""Representative hosted-runtime check using an original 45-page manuscript."""
import argparse
import json
import os
from pathlib import Path
import tempfile
import time

import httpx
import pymupdf
from benchmark_hosted import manuscript


def verify(base, model="validated-lexical"):
    with tempfile.TemporaryDirectory(prefix="synthetic-runtime-") as temp, httpx.Client(base_url=base, timeout=90) as client:
        target = Path(temp) / "synthetic45.pdf"
        manuscript(target)
        session = client.post("/api/public/session", json={"email": os.environ["BUNA_TEST_EMAIL"]})
        session.raise_for_status()
        auth = {"Authorization": "Bearer " + session.json()["token"]}
        library = client.get("/api/public/library", headers=auth).json()["papers"]
        assert len(library) == 44
        result = client.post("/api/public/jobs", headers=auth,
                             data={"selected": json.dumps([paper["sha256"] for paper in library]), "comparison_model": model},
                             files={"target": ("original-synthetic45.pdf", target.read_bytes(), "application/pdf")})
        result.raise_for_status()
        path = "/api/public/jobs/" + result.json()["id"]
        started = time.monotonic()
        while time.monotonic() - started < 810:
            response = client.get(path, headers=auth)
            response.raise_for_status()
            state = response.json()
            if state["status"] != "running":
                break
            time.sleep(4)
        assert state["status"] == "complete", {key: state.get(key) for key in ("status", "error_code", "progress")}
        evidence = client.get(path + "/report.json", headers=auth).json()
        coverage = evidence["source_coverage"]
        assert state["checked"] == 44 and state["total"] == 44
        if model == "classified-v1.1":
            assert all(row["status"] == "compared" and row["exact_scan_complete"] and row["similar_scan_complete"] for row in coverage)
            assert evidence["algorithm_version"] == model
            assert evidence["metrics"]["overlapping_words"] == evidence["metrics"]["exact_words"] + evidence["metrics"]["similar_only_words"]
        else:
            assert all(row["status"] == "compared" and row["source_windows_visited"] == row["source_windows_total"] for row in coverage)
        pdf = client.get(path + "/report.pdf", headers=auth)
        pdf.raise_for_status()
        with pymupdf.open(stream=pdf.content, filetype="pdf") as document:
            assert len(document) >= 45
        proof = {"pages": 45, "sources": 44, "fully_checked": 44, "model": model, "pdf_bytes": len(pdf.content),
                 "seconds": round(time.monotonic() - started, 2),
                 "runtime": evidence["hosted_runtime"], "progress": state["progress"]}
        client.delete(path, headers=auth).raise_for_status()
        bad = client.post("/api/public/jobs", headers=auth,
                          data={"selected": json.dumps([library[0]["sha256"]])},
                          files={"target": ("malformed.pdf", b"%PDF-broken synthetic fixture", "application/pdf")})
        bad.raise_for_status()
        bad_path = "/api/public/jobs/" + bad.json()["id"]
        for _ in range(30):
            state = client.get(bad_path, headers=auth).json()
            if state["status"] != "running":
                break
            time.sleep(1)
        assert state["status"] == "failed" and state["error_code"] == "parse-manuscript-invalid", state
        proof["malformed_input_specific_error"] = True
        client.delete(bad_path, headers=auth).raise_for_status()
        return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--model", choices=["validated-lexical", "classified-v1.1"], default="validated-lexical")
    args = parser.parse_args()
    print(json.dumps(verify(args.base, args.model)))
