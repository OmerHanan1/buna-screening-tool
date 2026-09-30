"""Original 200-word fixture for the versioned manuscript-only denominator."""
import json
import os
import re
import time
from uuid import uuid4

import httpx
import pymupdf

PHRASE = "amber birds gather beside quiet rivers during winter mornings while copper sensors record signals"


def fixtures():
    front = " ".join(f"frontword{i}" for i in range(20))
    body = ("Abstract\n" + PHRASE + ".\n\n" + " ".join(f"uniqueword{i}" for i in range(125)) + ".\n\n"
            + '"' + " ".join(f"quoteword{i}" for i in range(10)) + '"\n\nReferences\n'
            + " ".join(f"referenceword{i}" for i in range(29)))
    with pymupdf.open() as document:
        for text in (front, body):
            page = document.new_page()
            assert page.insert_textbox(pymupdf.Rect(40, 40, 550, 780), text, fontsize=9) >= 0
        target = document.tobytes()
    sources = []
    for suffix in ("First", "Second"):
        with pymupdf.open() as document:
            page = document.new_page()
            assert page.insert_textbox(pymupdf.Rect(40, 40, 550, 780), "Introduction\n" + PHRASE + ".\n" + suffix + " independent ending.", fontsize=9) >= 0
            sources.append(document.tobytes())
    return target, sources


def verify(base="http://127.0.0.1:8080", model="validated-lexical", eligible_policy=True):
    with httpx.Client(base_url=base, timeout=90) as client:
        origin = os.environ.get("BUNA_TEST_ORIGIN")
        if origin:
            client.headers["Origin"] = origin
        response = client.post("/api/public/session", json={"email": os.environ["BUNA_TEST_EMAIL"]})
        response.raise_for_status()
        headers = {"Authorization": "Bearer " + response.json()["token"]}
        response = client.get("/api/public/library", headers=headers)
        response.raise_for_status()
        papers = response.json()["papers"]
        assert len(papers) >= 55
        target, sources = fixtures()
        response = client.post("/api/public/jobs", headers={**headers, "Idempotency-Key": str(uuid4())},
                               data={"selected": json.dumps([paper["sha256"] for paper in papers[:55]]), "comparison_model": model},
                               files=[("target", ("Original synthetic eligible-policy target.pdf", target, "application/pdf"))]
                                     + [("sources", (f"Original synthetic policy source {i}.pdf", source, "application/pdf"))
                                        for i, source in enumerate(sources)])
        response.raise_for_status()
        path = "/api/public/jobs/" + response.json()["id"]
        started = time.monotonic()
        try:
            for _ in range(260):
                response = client.get(path, headers=headers)
                response.raise_for_status()
                state = response.json()
                if state["status"] != "running":
                    break
                time.sleep(3)
            assert state["status"] == "complete" and state["checked"] == state["total"] == 57 and not state["partial"], state
            response = client.get(path + "/report.json", headers=headers)
            response.raise_for_status()
            evidence = response.json()
            metrics = evidence["metrics"]
            denominator = 140 if eligible_policy else 180
            expected = 10.0 if eligible_policy else 7.78
            assert metrics["total_words"] == 200 and metrics["front_matter_words"] == 20, metrics
            assert metrics["eligible_words"] == 140 and metrics["score_denominator_words"] == denominator, metrics
            assert metrics["overlapping_words"] == 14 and metrics["overlap_percent"] == state["overlap_percent"] == expected, metrics
            if eligible_policy:
                assert metrics["score_policy_version"] == "eligible-manuscript-v1"
                assert state["word_accounting"]["score_denominator_words"] == 140
                assert metrics["excluded_bibliography_words"] == 30 and metrics["excluded_quotation_words"] == 10
            for row in evidence["source_coverage"]:
                assert row["score_denominator_words"] == denominator
                assert row["overlap_percent"] == round(100 * row["overlapping_words"] / denominator, 2)
            assert evidence["source_coverage"][-1]["overlapping_words"] == evidence["source_coverage"][-2]["overlapping_words"] == 14
            response = client.get(path + "/report.pdf", headers=headers)
            response.raise_for_status()
            with pymupdf.open(stream=response.content, filetype="pdf") as pdf:
                start = next(row[2] for row in pdf.get_toc() if row[1] == "Annotated manuscript") - 1
                text = "\n".join(pdf[i].get_text() for i in range(start))
                assert re.findall(r"(?m)^#(\d+)\s*$", text) == list(map(str, range(1, 58)))
                assert f"{expected:.2f}%" in text
                if eligible_policy:
                    assert "eligible manuscript words after exclusions" in text
                    assert "140 eligible" in text
                if model == "classified-v1.1":
                    assert metrics["exact_percent"] == expected
                    assert evidence["classification"]["metrics"]["combined_percent"] == expected
            return {"model": model, "score_policy": metrics["score_policy_version"], "selected": 57, "checked": 57,
                    "total": 200, "front": 20, "bibliography": 30, "quoted": 10, "eligible": 140,
                    "denominator": denominator, "unique_matched": 14, "overlap_percent": expected,
                    "all_source_percentages_verified": True, "pdf_source_rows": 57,
                    "pdf_bytes": len(response.content), "seconds": round(time.monotonic() - started, 2),
                    "synthetic_job_deleted": True, "no_library_mutations": True}
        finally:
            client.delete(path, headers=headers).raise_for_status()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--model", default="validated-lexical")
    parser.add_argument("--legacy-policy", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.base, args.model, not args.legacy_policy)))
