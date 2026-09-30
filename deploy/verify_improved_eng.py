"""Original synthetic improvedEng release gate; no corpus or library access.

Frozen fixture rubric: exact equality of actual manuscript matched-word sets,
per-source and overall, is required. Scores alone cannot pass this gate.
This is construction-based correctness evidence, not real-paper accuracy.
"""
import argparse
import json
from pathlib import Path
import re
import resource
import sys
import tempfile
import time

import pymupdf

from buna.documents import _structure
from buna.improved_eng import improved_report
from buna.pdf_reports import generate_pdf


EXACT = "amber birds gather beside quiet rivers during winter mornings while copper sensors record signals".split()
NEAR = "we used a linear mixed effects model to test whether disgust type affected participants responses".split()
SOURCE_NEAR = "we used a linear mixed effects model to examine the effect of disgust type on participants responses".split()
NEAR_EQUAL = {0, 1, 2, 3, 4, 5, 6, 7, 10, 11, 13, 14}


def document(words):
    return _structure([{"number": i // 600 + 1, "text": " ".join(words[i:i + 600])}
                       for i in range(0, len(words), 600)], "Original synthetic", [])


def verify(count=57, words=12000, pdf=True):
    if count < 2 or words < 600:
        raise ValueError("Use at least two sources and 600 words.")
    target_words = [f"targetword{i}" for i in range(words)]
    target_words[0] = "Abstract"
    target_words[100:100 + len(EXACT)] = EXACT
    target_words[300:300 + len(NEAR)] = NEAR
    target = document(target_words)
    sources = [{"id": str(i), "source_number": i, "title": f"Original synthetic source {i:03}",
                "status": "parsed"} for i in range(1, count + 1)]
    loads = 0

    def load(source):
        nonlocal loads
        loads += 1
        i = int(source["id"])
        source_words = [f"source{i}word{j}" for j in range(words)]
        if i == 1:
            source_words[100:100 + len(EXACT)] = EXACT
        if i == 2:
            source_words[300:300 + len(SOURCE_NEAR)] = SOURCE_NEAR
        return document(source_words)

    started = time.monotonic()
    result = improved_report(target, sources, load_document=load, total_time_limit_seconds=480)
    elapsed = time.monotonic() - started
    expected = {"1": set(range(100, 100 + len(EXACT))), "2": {300 + i for i in NEAR_EQUAL}}
    observed = {s["id"]: set() for s in sources}
    for match in result["matches"]:
        observed[match["source_id"]].update(match["scored_word_positions"])
    for source in sources:
        assert observed[source["id"]] == expected.get(source["id"], set()), (source["id"], observed[source["id"]])
    gold = set.union(*expected.values())
    assert result["metrics"]["overlapping_words"] == len(gold)
    assert result["metrics"]["score_denominator_words"] == words
    assert all(row["status"] == "compared" for row in result["source_coverage"])
    assert not result["metrics"]["truncated"]
    assert result["comparison_model"] == "improvedEng"
    assert result["metrics"]["score_policy_version"] == "eligible-manuscript-v1"
    assert loads == count
    result["papers"] = sources
    pdf_result = {}
    if pdf:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic-improved.pdf"
            mapping = generate_pdf({"job": {"filename": "Original synthetic target.txt", "document": target},
                                    "report": result}, path)
            assert not mapping["unmapped_regions"], mapping
            with pymupdf.open(path) as output:
                summary = "\n".join(output[i].get_text() for i in range(mapping["summary_pages"]))
                ids = re.findall(r"(?m)^#(\d+)\s*$", summary)
                assert ids == [str(i) for i in range(1, count + 1)], ids
                normalized = re.sub(r"\s+", "", summary)
                assert "improvedEng" in summary
                assert "E·Exactoverlap" in normalized and "S·Similarwording" in normalized
                assert "editsorreordering" not in normalized
                annotation_count = sum(len(list(page.annots() or [])) for page in output)
                assert annotation_count
                pdf_result = {"all_source_rows_once": True, "annotations": annotation_count,
                              "summary_pages": mapping["summary_pages"], "bytes": path.stat().st_size}
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mib = rss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    return {"model": "improvedEng", "sources": count, "words_per_document": words, "source_loads": loads,
            "comparison_seconds": round(elapsed, 3), "peak_process_rss_mib": round(rss_mib, 2),
            "fully_checked": count, "scored_union_words": len(gold), "eligible_words": words,
            "source_specific_word_precision": 1.0, "source_specific_word_recall": 1.0,
            "fixture_only_not_vendor_accuracy": True, "pdf": pdf_result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=int, default=57)
    parser.add_argument("--words", type=int, default=12000)
    parser.add_argument("--no-pdf", action="store_true")
    args = parser.parse_args()
    print(json.dumps(verify(args.sources, args.words, not args.no_pdf)))
