import copy

import pytest

from buna.documents import _structure
from buna.improved_eng import improved_report
from buna.span_evaluation import evaluate, evaluate_passages


def doc(text):
    return _structure([{"number": 1, "text": text}], "Synthetic", [])


def fixture():
    words = [f"token{i}" for i in range(40)]
    source = doc(" ".join(words) + " ending")
    report = improved_report(doc(" ".join(words)), [{"id": "1", "document": source}])
    return report, source


def gold(report, passages):
    return {"annotation_complete": True,
            "manuscript_ledger_sha256": report["improved_eng"]["manuscript_ledger_sha256"],
            "source_manifest": {r["source_id"]: r["source_content_sha256"] for r in report["source_coverage"]},
            "exclusions": {key: report["settings"][key] for key in
                           ("score_policy_version", "exclude_quotes", "manuscript_scope")},
            "passages": passages}


def label(ma, mb, sa, sb):
    return {"source_id": "1", "word_start": ma, "word_end": mb, "source_word_start": sa, "source_word_end": sb}


def test_small_boundaries_are_shared_not_complete_false_positive_negative():
    report, _ = fixture()
    result = evaluate_passages(report, gold(report, [label(1, 39, 1, 39)]), 1)
    assert result["counts"] == {"A": 1, "B": 0, "C": 0, "D": 0}
    assert result["word_metrics"]["counts"]["detector_only"] == 2


def test_same_source_passage_larger_highlight_is_partial_not_two_whole_errors():
    report, _ = fixture()
    result = evaluate_passages(report, gold(report, [label(0, 15, 0, 15)]), 1)
    assert result["counts"] == {"A": 0, "B": 0, "C": 0, "D": 1}
    diagnostic = result["categories"]["D"][0]["diagnostics"][0]
    assert diagnostic["matched_non_citation_words"] == 40
    assert diagnostic["longest_exact_run"] == 40
    assert diagnostic["manuscript_density"] == diagnostic["source_density"] == 1


def test_reference_only_actual_diagnostic_and_detector_only():
    m = "a b c d e f g h " + " ".join(f"m{i}" for i in range(6)) + " alpha beta gamma delta epsilon zeta eta theta iota"
    s = "a b c d e f g h " + " ".join(f"s{i}" for i in range(6)) + " alpha beta gamma delta epsilon zeta eta theta iota ending"
    source = doc(s)
    report = improved_report(doc(m), [{"id": "1", "document": source}])
    labels = gold(report, [label(0, 8, 0, 8)])
    missing = evaluate_passages(report, labels, 1)
    assert missing["counts"] == {"A": 0, "B": 1, "C": 1, "D": 0}
    assert not missing["diagnostic_completeness"] and not missing["calibration_complete"]
    result = evaluate_passages(report, labels, 1, {"1": source})
    assert result["diagnostic_completeness"]
    probe = result["categories"]["B"][0]["diagnostics"][0]
    assert probe["status"] == "computed" and probe["matched_non_citation_words"] == 8
    assert probe["seed"]["words"] == ["a", "b", "c"]
    assert probe["manuscript_span"] == probe["source_span"] == [0, 8]
    assert result["categories"]["C"][0]["diagnostics"][0]["matched_non_citation_words"] == 9


def test_wrong_source_occurrence_cannot_become_shared():
    report, _ = fixture()
    report["source_coverage"][0]["source_total_words"] = 100
    result = evaluate_passages(report, gold(report, [label(0, 40, 50, 90)]), 1)
    assert result["counts"] == {"A": 0, "B": 1, "C": 1, "D": 0}


def test_reference_probe_rejects_wrong_source_version():
    report, _ = fixture()
    report["source_coverage"][0]["source_total_words"] = 100
    labels = gold(report, [label(0, 10, 50, 60)])
    with pytest.raises(ValueError, match="content/version"):
        evaluate_passages(report, labels, 1, {"1": doc("wrong source")})


def test_cannot_calibrate_partial_or_missing_source_occurrences():
    report, _ = fixture()
    labels = gold(report, [label(0, 40, 0, 40)])
    with pytest.raises(ValueError, match="61"):
        evaluate_passages(report, labels)
    partial = copy.deepcopy(report)
    partial["source_coverage"][0]["status"] = "compared-with-limits"
    with pytest.raises(ValueError, match="fully compared"):
        evaluate_passages(partial, labels, 1)
    del labels["passages"][0]["source_word_start"]
    with pytest.raises(ValueError, match="source occurrence"):
        evaluate_passages(report, labels, 1)


def test_all_61_sources_complete_before_word_or_passage_metrics():
    text = " ".join(f"token{i}" for i in range(12))
    report = improved_report(doc(text), [{"id": str(i), "document": doc(text + " ending")} for i in range(61)])
    labels = gold(report, [{"source_id": str(i), "word_start": 0, "word_end": 12,
                            "source_word_start": 0, "source_word_end": 12} for i in range(61)])
    result = evaluate_passages(report, labels)
    assert result["counts"] == {"A": 61, "B": 0, "C": 0, "D": 0}
    assert result["word_metrics"]["precision"] == result["word_metrics"]["recall"] == 1
    assert evaluate(report, labels)["source_count"] == 61
