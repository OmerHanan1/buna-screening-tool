import copy
import json

import pytest

from buna.anchored_matching import anchored_paths, citation_mask, continuity
from buna.classified import tokens
from buna.documents import _structure
from buna.improved_eng import SearchLimit, improved_report
from buna import improved_eng
from buna.match_diagnostics import diagnostics_csv
from buna.span_evaluation import evaluate

PHRASE = "robust cortical neural signals predict distinct emotional responses among adult volunteers"


def doc(text):
    return _structure([{"number": 1, "text": text}], "Synthetic", [])


def compare(m, s):
    return improved_report(doc(m), [{"id": "1", "document": doc(s + " independent source ending")}])


def similar(result):
    return [m for m in result["matches"] if m["match_kind"] == "similar"]


def test_strong_anchor_isolated_substitution_survives():
    result = compare(PHRASE, PHRASE.replace("emotional", "affective"))
    assert similar(result)
    match = similar(result)[0]
    assert match["included_words"] == 10
    diagnostic = match["diagnostics"]
    assert diagnostic["anchor_length"] == 4
    assert diagnostic["longest_exact_run"] == 6
    assert diagnostic["number_of_gaps"] == diagnostic["maximum_gap"] == 1
    assert diagnostic["total_gap_words"] == 2
    assert diagnostic["local_minimum_density"] >= .70
    assert diagnostic["reason_match_terminated"]
    assert result["improved_eng"]["similar_diagnostics"]
    csv = diagnostics_csv(result)
    assert "source,source_content_sha256,manuscript_span" in csv
    assert "citation_token_contribution" in csv
    assert json.loads(json.dumps(result))["metrics"] == result["metrics"]


def test_no_content_anchor_despite_many_shared_stopwords():
    m = "we have a result and we have a value and we have a sample"
    s = "we have a finding and we have a value and we have a group"
    assert not similar(compare(m, s))


def test_exact_generic_long_matches_unchanged():
    text = "the results showed that the main effect was not statistically significant"
    result = compare(text, text)
    assert result["metrics"]["exact_words"] == len(text.split())


def test_v2_never_uses_the_frozen_permissive_path(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("v1 flexible search must not run")
    monkeypatch.setattr(improved_eng, "search", fail)
    assert similar(compare(PHRASE, PHRASE.replace("emotional", "affective")))


def test_long_exact_run_is_not_reexpanded_per_anchor():
    words = ["cortical", "neural", "robust", "distinct"] + [f"word{i}" for i in range(3000)]
    result = compare(" ".join(words), " ".join(words))
    assert result["metrics"]["exact_words"] == len(words)
    assert result["source_coverage"][0]["status"] == "compared"


@pytest.mark.parametrize("marker", ["(Smith et al., 2020)", "[12, 13]", "Smith et al. (2020)",
                                    "(Smith, 2020, pp. 23–25)", "Smith, Doe, and Roe (2020)"])
def test_citations_do_not_establish_or_qualify_but_can_be_bridged(marker):
    m = PHRASE.replace("predict", marker + " predict")
    result = compare(m, PHRASE)
    assert similar(result)
    item = similar(result)[0]
    assert item["included_words"] == len(PHRASE.split())
    cited = citation_mask(m, tokens(m))
    assert any(cited)
    assert not any(cited[i] for i in item["scored_word_positions"])
    assert item["diagnostics"]["citation_token_contribution"]["minimum_match"] == 0
    assert item["diagnostics"]["citation_token_contribution"]["density"] == 0
    assert item["diagnostics"]["citation_token_contribution"]["manuscript_tokens_inside"] > 0
    short = "robust cortical neural signals " + marker + " predict values"
    assert not similar(compare(short, short.replace("values", "outcomes")))


def test_numbers_literal_isolated_mismatch_does_not_kill_strong_prose():
    result = compare(PHRASE.replace("among", "4.72 among"), PHRASE.replace("among", "4.16 among"))
    assert similar(result)
    assert all("4.72" not in match["manuscript"]["text"][a:b] for match in similar(result)
               for a, b in match["manuscript"]["highlights"])


def test_commonness_uses_frequency_not_phrase_blacklist():
    m = (PHRASE + " unmatched ") * 4
    s = (PHRASE.replace("emotional", "affective") + " unrelated ") * 4
    assert not similar(compare(m, s))


def test_generic_only_window_cannot_bridge_a_flexible_match():
    path = tuple((i, i) for i in range(24))
    informative = [True] * 4 + [False] * 16 + [True] * 4
    info, reason = continuity(path, list(range(24)), list(range(24)), (informative, informative))
    assert info["local_minimum_density"] == 1
    assert info["local_minimum_informative_matches"] == [0, 0]
    assert reason == "generic-bridge"


def test_weak_middle_cannot_be_compensated_by_strong_ends():
    m = "robust cortical neural signals predict distinct emotional responses among adult volunteers"
    s = "robust cortical neural signals predict alpha beta gamma delta epsilon adult volunteers"
    assert not similar(compare(m, s))
    path = tuple((i, i) for i in range(12)) + tuple((i + 4, i + 4) for i in range(12, 30))
    info, reason = continuity(path, list(range(40)), list(range(40)))
    assert info["global_density"] > .8
    assert info["local_minimum_density"] < .7
    assert reason in {"maximum-gap", "local-density"}


def test_repeated_small_gaps_terminate_instead_of_indefinite_chain():
    path = tuple((i + i // 4, i) for i in range(24))
    info, reason = continuity(path, list(range(40)), list(range(40)))
    assert info["global_density"] > .7
    assert info["number_of_gaps"] == 5
    assert reason == "gap-event-budget"


def test_all_moving_windows_and_partial_windows_are_checked():
    path = tuple((i, i) for i in range(8)) + tuple((i + 3, i + 3) for i in range(8, 19))
    info, _ = continuity(path, list(range(30)), list(range(30)))
    assert info["local_minimum_density"] == .75
    short = tuple((i, i) for i in range(4)) + ((6, 6),)
    info, reason = continuity(short, list(range(10)), list(range(10)))
    assert info["local_minimum_density"] == 5 / 7 and reason is None


def test_eligible_boundaries_and_source_occurrences():
    source = PHRASE.replace("emotional", "affective")
    result = compare(PHRASE, source + " " + "unrelated " * 20 + source)
    assert len(similar(result)) == 2
    quote = compare(PHRASE.replace("predict distinct emotional", '"predict distinct emotional"'), source)
    assert not similar(quote)


def test_profile_8_reject_9_accept():
    for count in (8, 9):
        words = PHRASE.split()[:count + 1]
        source = words.copy()
        source[6] = "different"
        result = compare(" ".join(words), " ".join(source))
        assert bool(similar(result)) is (count == 9)


def test_diagnostics_csv_neutralizes_formula_sources():
    assert "'=formula" in diagnostics_csv({"improved_eng": {"similar_diagnostics": [{"source": "=formula"}]}})


def gold_for(report):
    return {
        "annotation_complete": True,
        "manuscript_ledger_sha256": report["improved_eng"]["manuscript_ledger_sha256"],
        "source_manifest": {r["source_id"]: r["source_content_sha256"] for r in report["source_coverage"]},
        "exclusions": {key: report["settings"][key] for key in
                       ("score_policy_version", "exclude_quotes", "manuscript_scope")},
        "passages": [{"source_id": "1", "word_start": 0, "word_end": 8}],
    }


def test_four_sets_and_source_specific_word_metrics():
    result = compare(PHRASE + " unmatched", PHRASE.replace("emotional", "affective"))
    evaluation = evaluate(result, gold_for(result), expected_source_count=1)
    assert evaluation["counts"] == {
        "crossref_and_detector": 7, "crossref_only": 1, "detector_only": 3, "neither": 1}
    assert evaluation["precision"] == .7
    assert evaluation["recall"] == 7 / 8
    assert evaluation["sources"]["1"]["word_spans"]["crossref_only"] == [[6, 7]]


def test_evaluation_refuses_partial_wrong_manifest_scope_and_ledger():
    result = compare(PHRASE, PHRASE.replace("emotional", "affective"))
    gold = gold_for(result)
    with pytest.raises(ValueError, match="61"):
        evaluate(result, gold)
    for mutate in (
        lambda r: r["source_coverage"][0].update(status="compared-with-limits"),
        lambda r: r["improved_eng"].update(manuscript_ledger_sha256="wrong"),
        lambda r: r["source_coverage"][0].update(source_content_sha256="wrong"),
        lambda r: r["settings"].update(exclude_quotes=False),
    ):
        modified = copy.deepcopy(result)
        mutate(modified)
        with pytest.raises(ValueError):
            evaluate(modified, gold, expected_source_count=1)


def test_all_61_source_evaluation_with_full_frozen_synthetic_labels():
    sources = [{"id": str(i), "document": doc(PHRASE.replace("emotional", "affective") + " ending")}
               for i in range(61)]
    result = improved_report(doc(PHRASE), sources)
    gold = gold_for(result)
    gold["passages"] = [{"source_id": s["id"], "word_start": start, "word_end": end}
                        for s in sources for start, end in ((0, 6), (7, 11))]
    value = evaluate(result, gold)
    assert value["source_count"] == 61 and value["calibration_complete"]
    assert value["precision"] == value["recall"] == 1
    assert value["counts"] == {"crossref_and_detector": 610, "crossref_only": 0,
                               "detector_only": 0, "neither": 61}
