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


def _reference_probe(report, reference, documents, eligible):
    """Diagnostic-only LCS inside explicit, source-pinned reference bounds."""
    if documents is None:
        return {"status": "unavailable", "reason": "Actual source document required; no alignment was invented."}
    from buna.classified import tokens
    from buna.citation_tokens import citation_mask
    from buna.comparison import _line_join_words, _intervals, _mask
    from buna.match_diagnostics import alignment_details
    source = documents.get(reference["source_id"])
    if not isinstance(source, dict) or not isinstance(source.get("text"), str):
        raise ValueError("A reference probe requires the original extracted source document.")
    manifest = {r["source_id"]: r["source_content_sha256"] for r in report["source_coverage"]}
    if hashlib.sha256(source["text"].encode()).hexdigest() != manifest[reference["source_id"]]:
        raise ValueError("Reference probe source content/version differs from this comparison.")
    manuscript = "\n\n".join(page["text"] for page in report["manuscript_pages"])
    mt = tokens(manuscript, _line_join_words([manuscript]))
    if ledger_sha256(mt) != report["improved_eng"]["manuscript_ledger_sha256"]:
        raise ValueError("Cannot reconstruct the original manuscript ledger for the reference probe.")
    st = tokens(source["text"], _line_join_words([source["text"]]))
    ma, mb = reference["word_start"], reference["word_end"]
    sa, sb = reference["source_word_start"], reference["source_word_end"]
    if not 0 <= sa < sb <= len(st):
        raise ValueError("Reference source word bounds are outside the original source.")
    if (mb - ma + 1) * (sb - sa + 1) > 200_000:
        return {"status": "unavailable", "reason": "Diagnostic alignment exceeds its explicit 200,000-cell work bound; narrow the labeled passage."}
    mc, sc = citation_mask(manuscript, mt), citation_mask(source["text"], st)
    excluded_source = _mask(st, _intervals(source))
    dp = [[0] * (sb - sa + 1) for _ in range(mb - ma + 1)]
    for a in range(mb - ma - 1, -1, -1):
        for b in range(sb - sa - 1, -1, -1):
            equal = (ma + a in eligible and not excluded_source[sa + b] and not mc[ma + a] and not sc[sa + b]
                     and mt[ma + a][0] == st[sa + b][0])
            dp[a][b] = 1 + dp[a + 1][b + 1] if equal else max(dp[a + 1][b], dp[a][b + 1])
    a = b = 0
    path = []
    while a < mb - ma and b < sb - sa:
        equal = (ma + a in eligible and not excluded_source[sa + b] and not mc[ma + a] and not sc[sa + b]
                 and mt[ma + a][0] == st[sa + b][0])
        if equal:
            path.append((ma + a, sa + b))
            a, b = a + 1, b + 1
        elif dp[a + 1][b] >= dp[a][b + 1]:
            a += 1
        else:
            b += 1
    if not path:
        return {"status": "no-equal-prose", "source": reference["source_id"],
                "matched_non_citation_words": 0, "longest_exact_run": 0, "seed": None,
                "manuscript_span": [ma, mb], "source_span": [sa, sb],
                "manuscript_density": 0, "source_density": 0,
                "manuscript_gap_sequence": [], "source_gap_sequence": [],
                "citation_matches": {"count": 0, "aligned_pairs": []},
                "citation_tokens_inside_span": {"manuscript": sum(mc[ma:mb]), "source": sum(sc[sa:sb])},
                "reason": "No equal eligible noncitation words in these actual labeled manuscript/source ranges."}
    return {"status": "computed", "source": reference["source_id"],
            "basis": "Diagnostic-only deterministic LCS in labeled bounds; not a substitute for detector alternative search.",
            "crosses_exclusion_boundary": (any(i not in eligible for i in range(path[0][0], path[-1][0] + 1))
                                          or any(excluded_source[path[0][1]:path[-1][1] + 1])),
            **alignment_details(tuple(path), [w[0] for w in mt], [w[0] for w in st], mc, sc)}


def evaluate_passages(report, gold, expected_source_count=61, source_documents=None):
    word_metrics = evaluate(report, gold, expected_source_count)
    eligible = {i for item in report["classification"]["coverage_intervals"] if not item["state"].startswith("excluded")
                for i in range(item["word_start"], item["word_end"])}
    references = [ref for ref in gold["passages"] if any(i in eligible for i in range(ref["word_start"], ref["word_end"]))]
    predictions = [m for m in report["matches"] if not m.get("excluded_from_score") and set(m["scored_word_positions"]) & eligible]
    source_lengths = {r["source_id"]: r.get("source_total_words") for r in report["source_coverage"]}
    for ref in references:
        if not all(isinstance(ref.get(key), int) for key in ("source_word_start", "source_word_end")):
            raise ValueError("Passage agreement requires source occurrence word bounds for every reference passage.")
        if (not 0 <= ref["source_word_start"] < ref["source_word_end"]
                or (source_lengths[ref["source_id"]] is not None
                    and ref["source_word_end"] > source_lengths[ref["source_id"]])):
            raise ValueError("Reference source occurrence bounds are invalid.")

    def overlap_smaller(a, b):
        return max(0, min(a[1], b[1]) - max(a[0], b[0])) / min(a[1] - a[0], b[1] - b[0])

    edges = {("reference", i): set() for i in range(len(references))}
    edges.update({("detector", i): set() for i in range(len(predictions))})
    for i, ref in enumerate(references):
        mspan, sspan = (ref["word_start"], ref["word_end"]), (ref["source_word_start"], ref["source_word_end"])
        for j, match in enumerate(predictions):
            if ref["source_id"] != match["source_id"]:
                continue
            if (overlap_smaller(mspan, (match["manuscript"]["word_start"], match["manuscript"]["word_end"])) >= .5
                    and overlap_smaller(sspan, (match["source"]["word_start"], match["source"]["word_end"])) >= .5):
                edges["reference", i].add(("detector", j))
                edges["detector", j].add(("reference", i))
    categories = {key: [] for key in ("A", "B", "C", "D")}
    visited = set()
    for node in edges:
        if node in visited:
            continue
        pending, group = [node], set()
        while pending:
            current = pending.pop()
            if current in group:
                continue
            group.add(current)
            pending.extend(edges[current] - group)
        visited.update(group)
        refs = [references[i] for kind, i in sorted(group) if kind == "reference"]
        detected = [predictions[i] for kind, i in sorted(group) if kind == "detector"]
        reference_words = {i for ref in refs for i in range(ref["word_start"], ref["word_end"])} & eligible
        detected_words = {i for m in detected for i in m["scored_word_positions"]} & eligible
        if refs and detected:
            common = reference_words & detected_words
            union = reference_words | detected_words
            small_boundary = bool(common) and (
                abs(min(reference_words) - min(detected_words)) <= 2
                and abs(max(reference_words) - max(detected_words)) <= 2
                and all(i < min(common) or i > max(common) for i in reference_words ^ detected_words))
            category = "A" if small_boundary or len(common) / len(union) >= .8 else "D"
        else:
            category = "B" if refs else "C"
        item = {
            "source": refs[0]["source_id"] if refs else detected[0]["source_id"],
            "reference_passages": refs,
            "detector_match_indices": [i for kind, i in sorted(group) if kind == "detector"],
            "reference_words": len(reference_words), "detector_words": len(detected_words),
            "shared_words": len(reference_words & detected_words),
            "diagnostics": [m.get("diagnostics", {"status": "unavailable", "reason": "Saved report lacks actual alignment diagnostics."})
                            for m in detected],
        }
        if category == "B":
            item["diagnostics"] = [_reference_probe(report, ref, source_documents, eligible) for ref in refs]
        categories[category].append(item)
    diagnostics_complete = all(d.get("status") != "unavailable" for key in ("B", "C", "D")
                               for item in categories[key] for d in item["diagnostics"])
    return {
        "metric_version": "source-linked-passage-agreement-v1", "source_comparison_complete": True,
        "calibration_complete": diagnostics_complete,
        "categories": categories, "counts": {key: len(value) for key, value in categories.items()},
        "labels": {"A": "Crossref + improvedEng", "B": "Crossref only", "C": "improvedEng only", "D": "Partial disagreement"},
        "word_metrics": word_metrics,
        "evaluation_rubric": {"minimum_overlap_of_smaller_span_on_both_documents": .5,
                              "word_jaccard_agreement": .8, "small_boundary_tolerance_words": 2,
                              "not_detector_thresholds": True,
                              "D_is_partial_not_a_complete_false_positive_or_negative": True},
        "diagnostic_completeness": diagnostics_complete,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("gold", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--expected-sources", type=int, default=61)
    parser.add_argument("--source-documents", type=Path, help="JSON object of source IDs to original extracted documents for B probes.")
    parser.add_argument("--word-only", action="store_true", help="Export the older strict word sets without passage relationship grouping.")
    args = parser.parse_args()
    try:
        report, gold = json.loads(args.report.read_text()), json.loads(args.gold.read_text())
        if args.word_only:
            result = evaluate(report, gold, args.expected_sources)
        else:
            documents = json.loads(args.source_documents.read_text()) if args.source_documents else None
            result = evaluate_passages(report, gold, args.expected_sources, documents)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    args.destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
