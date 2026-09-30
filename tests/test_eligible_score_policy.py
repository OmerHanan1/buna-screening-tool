import copy
import pytest

from buna.classified import classify_report
from buna.improved_eng import improved_report
from buna.comparison import compare_documents, _tokens
from buna.documents import _structure
from buna.result_state import result_state
from buna.score_policy import manuscript_word_accounting, SCORE_BASIS, SCORE_POLICY_VERSION

MATCH = "amber birds gather beside quiet rivers during winter mornings while copper sensors record signals"


def document(*pages):
    return _structure([{"number": i + 1, "text": page} for i, page in enumerate(pages)], "Synthetic", [])


def counted_fixture():
    front = " ".join(f"frontword{i}" for i in range(20))
    body = "Abstract\n" + MATCH + ".\n\n" + " ".join(f"uniqueword{i}" for i in range(125)) + "."
    quotes = '"' + " ".join(f"quoteword{i}" for i in range(10)) + '"'
    bib = "References\n" + " ".join(f"referenceword{i}" for i in range(29))
    target = document(front, body + "\n\n" + quotes + "\n\n" + bib)
    assert len(_tokens(target["text"])) == 200
    return target


@pytest.mark.parametrize("engine", [compare_documents, classify_report, improved_report])
@pytest.mark.parametrize("exclude_quotes,expected", [(True, 140), (False, 150)])
def test_shared_eligible_denominator_and_duplicate_source_union(engine, exclude_quotes, expected):
    target = counted_fixture()
    sources = [{"id": "a", "document": document("Source prefix. " + MATCH + ". Independent ending.")},
               {"id": "b", "document": document("Different source prefix. " + MATCH + ". Unrelated ending.")}]
    result = engine(target, sources, exclude_quotes=exclude_quotes)
    metrics = result["metrics"]
    assert metrics["total_words"] == 200
    assert metrics["front_matter_words"] == 20
    assert metrics["scoped_words"] == metrics["analyzed_words"] == 180
    assert metrics["excluded_bibliography_words"] == 30
    assert metrics["excluded_quotation_words"] == (10 if exclude_quotes else 0)
    assert metrics["eligible_words"] == metrics["score_denominator_words"] == expected
    assert metrics["overlapping_words"] == 14
    assert metrics["overlap_percent"] == round(1400 / expected, 2)
    assert metrics["score_basis"] == SCORE_BASIS and metrics["score_policy_version"] == SCORE_POLICY_VERSION
    assert result["settings"]["score_policy_version"] == SCORE_POLICY_VERSION
    for row in result["source_coverage"]:
        assert row["overlapping_words"] == 14 and row["score_denominator_words"] == expected
        assert row["overlap_percent"] == metrics["overlap_percent"]
    if engine is classify_report:
        classified = result["classification"]["metrics"]
        assert classified["score_denominator_words"] == expected
        assert classified["combined_percent"] == metrics["overlap_percent"]
        assert classified["exact_percent"] == metrics["overlap_percent"]
        assert classified["similar_only_percent"] == 0


def test_mask_union_no_double_subtraction_and_other_declared_mask():
    # Front and bibliography overlap, quotes overlap both, one independent other mask.
    front = [True, True, False, False, False, False, False, False]
    bib = [False, True, True, False, False, False, False, False]
    quote = [True, False, True, True, False, False, False, False]
    eligible = [False, False, False, False, False, True, True, True]
    result = manuscript_word_accounting(eligible, front, bib, quote, exclude_quotes=True)
    assert result["score_denominator_words"] == 3
    assert result["front_matter_words"] == 2 and result["excluded_bibliography_words"] == 1
    assert result["excluded_quotation_words"] == 1 and result["other_excluded_manuscript_words"] == 1
    assert result["excluded_manuscript_words"] == 5


@pytest.mark.parametrize("engine", [compare_documents, classify_report, improved_report])
def test_all_excluded_unscorable_and_source_failures_do_not_reduce_denominator(engine):
    excluded = engine(document('"' + MATCH + '"'), [{"id": "source", "document": document(MATCH + ". independent")}])
    assert excluded["metrics"]["score_denominator_words"] == 0
    assert excluded["metrics"]["overlap_percent"] is None
    assert result_state(excluded)["state"] == "unscorable"
    assert not result_state(excluded)["score_available"]
    target = counted_fixture()
    result = engine(target, [{"id": "good", "document": document(MATCH + ". extra")},
                             {"id": "unavailable"}, {"id": "excluded", "excluded": True}])
    assert result["metrics"]["score_denominator_words"] == 140
    assert result["metrics"]["overlap_percent"] == 10
    result["papers"] = [{"id": row["source_id"], "status": row["status"]} for row in result["source_coverage"]]
    assert result_state(result)["state"] == "partial"


@pytest.mark.parametrize("engine", [compare_documents, classify_report, improved_report])
def test_no_abstract_fallback_and_minimum_match_filter_keep_unmatched_words(engine):
    target = document(" ".join(f"uniqueword{i}" for i in range(50)) + '\n\n"one two three four five"\n\nReferences\nreferenceword')
    result = engine(target, [{"id": "short", "document": document("uniqueword0 uniqueword1 uniqueword2 uniqueword3 end")}])
    assert result["metrics"]["score_denominator_words"] == 50
    assert result["metrics"]["overlapping_words"] == 0
    assert result["metrics"]["overlap_percent"] == 0
    assert any("Abstract heading not detected" in warning for warning in result["warnings"])


def test_old_saved_basis_remains_unchanged_when_presented():
    old = {"papers": [{"id": "s", "status": "compared"}], "metrics": {
        "total_words": 200, "eligible_words": 140, "score_denominator_words": 180,
        "score_basis": "abstract-onward-word-units", "score_policy_version": "abstract-onward-v1",
        "overlapping_words": 14, "overlap_percent": 7.78}}
    before = copy.deepcopy(old)
    assert result_state(old)["score_available"]
    assert old == before


def test_classified_exact_similar_union_uses_one_eligible_denominator():
    sentence = "Participants reported their age gender and political identity at the end of the online survey session"
    target = document("Cover and funding words.", "Abstract\n" + sentence
                      + '.\n\n"excluded quoted manuscript words are not scored here today"\n\nReferences\nExcluded source citation.')
    text = sentence.replace("political", "religious") + "."
    result = classify_report(target, [{"id": "a", "document": document("Source introduction. " + text)},
                                     {"id": "b", "document": document("Different introduction. " + text)}])
    metrics = result["metrics"]
    denominator = metrics["eligible_words"]
    assert metrics["exact_words"] > 0 and metrics["similar_only_words"] > 0
    assert metrics["overlapping_words"] == metrics["exact_words"] + metrics["similar_only_words"]
    assert metrics["overlap_percent"] == round(100 * metrics["overlapping_words"] / denominator, 2)
    assert metrics["exact_percent"] == round(100 * metrics["exact_words"] / denominator, 2)
    assert metrics["similar_only_percent"] == round(100 * metrics["similar_only_words"] / denominator, 2)
    for source in result["source_coverage"]:
        assert source["score_denominator_words"] == denominator
        assert source["exact_percent"] == round(100 * source["exact_words"] / denominator, 2)
        assert source["similar_only_percent"] == round(100 * source["similar_only_words"] / denominator, 2)
