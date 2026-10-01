"""Original dense excluded-audit fixture; no user manuscript or live library."""
import argparse
import json
from pathlib import Path
import resource
import sys
import tempfile
import time

from buna.documents import _structure
from buna.improved_eng import improved_report
from buna.pdf_reports import generate_pdf

PHRASE = "amber birds gather beside quiet rivers during winter mornings while copper sensors record signals"


def document(text):
    return _structure([{"number": 1, "text": text}], "Original synthetic memory fixture", [])


def verify(count=61, expect_complete=True):
    bibliography = " ".join(f"bibliographicword{i}" for i in range(1650))
    target = document("Abstract\n" + PHRASE + ".\n" + " ".join(f"targetword{i}" for i in range(10334))
                      + "\n\nReferences\n" + bibliography)
    sources = [{"id": str(i + 1), "source_number": i + 1, "title": f"Original synthetic source {i+1}",
                "status": "parsed"} for i in range(count)]
    def load(source):
        return document("Introduction\n" + PHRASE + ".\n"
                        + " ".join(f"source{source['id']}word{i}" for i in range(10334))
                        + "\n\nReferences\n" + bibliography)
    started = time.monotonic()
    result = improved_report(target, sources, load_document=load, total_time_limit_seconds=480)
    checked = sum(row["status"] == "compared" for row in result["source_coverage"])
    if expect_complete:
        assert checked == count and not result["metrics"]["truncated"]
        assert all(row["overlapping_words"] == 14 for row in result["source_coverage"])
        assert result["metrics"]["overlapping_words"] == 14
        assert result["improved_eng"]["audit_complete"] is False
        memory = result["improved_eng"]["evidence_memory"]
        assert memory["scored"]["limit_bytes"] + memory["excluded_audit"]["limit_bytes"] == 64 * 1024 * 1024
        assert memory["scored"]["retained_bytes"] + memory["excluded_audit"]["retained_bytes"] <= memory["combined_limit_bytes"]
    result["papers"] = sources
    report_bytes = len(json.dumps(result).encode())
    assert report_bytes < 64 * 1024 * 1024
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "report.pdf"
        mapping = generate_pdf({"job": {"filename": "Original synthetic memory target.txt", "document": target},
                                "report": result}, path)
        pdf_bytes = path.stat().st_size
    proof = {"sources": count, "checked": checked, "partial": result["metrics"]["truncated"],
             "scored_pairs_union": result["metrics"]["overlapping_words"],
             "eligible_words": result["metrics"]["score_denominator_words"],
             "audit_complete": result["improved_eng"]["audit_complete"],
             "memory": result["improved_eng"].get("evidence_memory"),
             "report_json_bytes": report_bytes, "pdf_bytes": pdf_bytes, "summary_pages": mapping["summary_pages"],
             "seconds": round(time.monotonic() - started, 2),
             "peak_rss_mib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)}
    print(json.dumps(proof), flush=True)
    return proof


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=int, default=61)
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    verify(args.sources, not args.baseline)
