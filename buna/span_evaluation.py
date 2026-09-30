"""Source-specific eligible-word agreement; refuses incomplete-corpus calibration."""
import argparse
import hashlib
import json
from pathlib import Path


def ledger_sha256(tokens):
    return hashlib.sha256(json.dumps(tokens, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def runs(words):
    result = []
    for word in sorted(words):
        if result and result[-1][1] == word:
            result[-1][1] += 1
        else:
            result.append([word, word + 1])
    return result


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def evaluate(report, gold, expected_source_count=61):
    """Gold intervals use the SAME saved word ledger, not PDF annotation bounds.

    schema: manuscript_ledger_sha256, source_manifest {id: extracted_text_sha256},
    exclusions {score_policy_version, exclude_quotes, manuscript_scope},
    passages [{source_id, word_start, word_end}]. Half-open word intervals.
    """
    rows = report.get("source_coverage", [])
    if gold.get("annotation_complete") is not True:
        raise ValueError("Reference annotation completeness must be explicitly confirmed before counting Neither.")
    if len(rows) != expected_source_count or any(r["status"] != "compared" for r in rows):
        raise ValueError(f"Calibration requires all {expected_source_count} sources fully compared; partial recall is not measurable.")
    if report["metrics"]["score_denominator_words"] <= 0:
        raise ValueError("Calibration requires eligible manuscript words after exclusions.")
    provenance = report.get("improved_eng", {})
    ledger = provenance.get("manuscript_ledger_sha256")
    if not ledger or ledger != gold.get("manuscript_ledger_sha256"):
        raise ValueError("Gold and detector must use the same original manuscript word ledger.")
    manifest = {r["source_id"]: r.get("source_content_sha256") for r in rows}
    if len(manifest) != expected_source_count or any(not h for h in manifest.values()) or manifest != gold.get("source_manifest"):
        raise ValueError("Source content/version manifest differs from the labeled comparison corpus.")
    settings = report["settings"]
    exclusions = {key: settings[key] for key in ("score_policy_version", "exclude_quotes", "manuscript_scope")}
    if exclusions != gold.get("exclusions"):
        raise ValueError("Gold exclusion policy/scope differs from the detector.")
    total = report["metrics"]["total_words"]
    eligible = set()
    for item in report["classification"]["coverage_intervals"]:
        if not item["state"].startswith("excluded"):
            eligible.update(range(item["word_start"], item["word_end"]))
    if len(eligible) != report["metrics"]["score_denominator_words"]:
        raise ValueError("Saved eligibility intervals do not match the denominator.")
    truth, prediction = {sid: set() for sid in manifest}, {sid: set() for sid in manifest}
    for item in gold["passages"]:
        sid, start, end = item["source_id"], item["word_start"], item["word_end"]
        if sid not in manifest or not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= total:
            raise ValueError("Gold passage has an unknown source or invalid word bounds.")
        truth[sid].update(set(range(start, end)) & eligible)
    for match in report["matches"]:
        if not match.get("excluded_from_score"):
            prediction[match["source_id"]].update(set(match["scored_word_positions"]) & eligible)
    totals = {"crossref_and_detector": 0, "crossref_only": 0, "detector_only": 0, "neither": 0}
    by_source = {}
    for sid in sorted(manifest):
        ref, detected = truth[sid], prediction[sid]
        sets = {"crossref_and_detector": ref & detected, "crossref_only": ref - detected,
                "detector_only": detected - ref, "neither": eligible - (ref | detected)}
        counts = {key: len(words) for key, words in sets.items()}
        for key, count in counts.items():
            totals[key] += count
        by_source[sid] = {"counts": counts, "word_spans": {key: runs(words) for key, words in sets.items()},
                         "precision": ratio(counts["crossref_and_detector"], len(detected)),
                         "recall": ratio(counts["crossref_and_detector"], len(ref))}
    tp, fp, fn = totals["crossref_and_detector"], totals["detector_only"], totals["crossref_only"]
    return {"metric_version": "source-specific-eligible-word-spans-v1",
            "source_count": expected_source_count, "calibration_complete": True,
            "counts": totals, "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + fn),
            "f1": ratio(2 * tp, 2 * tp + fp + fn), "sources": by_source,
            "notes": ["Four sets are source-specific eligible-word spans, not all-source score agreement.",
                      "Neither counts source-word opportunities; absence of a label assumes complete source-specific annotation.",
                      "Passage-level human agreement for slight same-sentence differences must be adjudicated separately."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("gold", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--expected-sources", type=int, default=61)
    args = parser.parse_args()
    try:
        result = evaluate(json.loads(args.report.read_text()), json.loads(args.gold.read_text()), args.expected_sources)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    args.destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
