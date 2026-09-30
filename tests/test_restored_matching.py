import itertools

import pytest

from buna.citation_tokens import citation_mask
from buna.classified import tokens
from buna.documents import _structure
from buna import improved_eng as engine
from buna.improved_eng import improved_report, search
from buna.match_diagnostics import diagnostics_csv


def doc(text):
    return _structure([{"number": 1, "text": text}], "Synthetic", [])


def compare(m, s):
    return improved_report(doc(m), [{"id": "1", "document": doc(s + " source ending")}])


def cited_paths(m, s):
    mt, st = tokens(m), tokens(s)
    return list(search([w for w, *_ in mt], [w for w, *_ in st], [True] * len(mt), [True] * len(st),
                       lambda: None, m_cited=citation_mask(m, mt), s_cited=citation_mask(s, st)))


def test_fragmented_v1_example_restored_with_actual_word_union():
    result = compare("a b c d x x e f g y h i j", "a b c d q e f g r h i j")
    assert result["algorithm_version"] == "improvedEng-v1-citation"
    assert result["metrics"]["overlapping_words"] == 10
    assert result["metrics"]["similar_only_words"] == 10
    assert result["matches"][0]["scored_word_positions"] == [0, 1, 2, 3, 6, 7, 8, 10, 11, 12]


def test_formulaic_language_and_literal_numbers_not_suppressed():
    m = "we used a linear mixed effects model to test whether disgust type affected participants responses"
    s = "we used a linear mixed effects model to examine the effect of disgust type on participants responses"
    result = compare(m, s)
    assert result["metrics"]["similar_only_words"] == 12
    numbers = compare("we used a linear mixed effects model 4.72 to report these results",
                      "we used a linear mixed effects model 4.16 to report these results")
    assert all(7 not in match["scored_word_positions"] for match in numbers["matches"])


def test_citation_tokens_are_not_graph_nodes_or_seed_words():
    m = "a b (Smith Jones Brown 2020) c d e f g"
    s = "a b (Smith Jones Brown 2020) c d e f x"
    assert not cited_paths(m, s)
    mt = tokens(m)
    mask = citation_mask(m, mt)
    assert sum(mask) == 4


@pytest.mark.parametrize("count,accepted", [(5, True), (6, False)])
def test_citations_consume_original_gap_space(count, accepted):
    prose = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
    marker = "(" + " ".join(["Smith"] * (count - 1)) + " 2020)"
    m = prose.replace("zeta", marker + " zeta")
    paths = cited_paths(m, prose)
    assert bool(paths) is accepted
    if accepted:
        assert len(paths[0]) == 10
        assert paths[0][-1][0] == 14
        assert max(b[0] - a[0] - 1 for a, b in zip(paths[0], paths[0][1:])) == 5


def test_citations_consume_span_and_cannot_inflate_density():
    plain = "a b c d e f g h i"
    m = "a b c (Smith Jones Brown 2020) d e f (Smith Brown 2021) g h i"
    assert not cited_paths(m, plain)  # 9/16 < .60 although each citation gap <=5.
    assert cited_paths(m.replace("Jones ", ""), plain)  # 9/15 == .60.


def test_citations_cannot_supply_ninth_match():
    m = "a b c d (Smith 2020) e f g h"
    s = "a b c d (Smith 2020) e f g other"
    assert not cited_paths(m, s)


def test_citation_only_seeds_do_not_retrieve_scattered_prose():
    m = "a x b y c z (Smith Jones Brown 2020) d u e v f w g r h q i"
    s = "a p b t c k (Smith Jones Brown 2020) d n e m f l g j h o i"
    assert not cited_paths(m, s)


def test_mixed_seed_does_not_receive_citation_credit():
    text = "a b (2020) c d (2021) e f (2022) g h (2023) i j"
    assert not cited_paths(text, text)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("masked_side", ["manuscript", "source", "both"])
def test_mixed_boundary_seed_rejected_in_original_coordinates(reverse, masked_side):
    m = "1 2 a x b c y d e z f g q h i".split()
    s = "1 2 a u b c v d e w f g r h i".split()
    mask = [True, True] + [False] * 13
    if reverse:
        m, s, mask = list(reversed(m)), list(reversed(s)), list(reversed(mask))
    paths = list(search(m, s, [True] * 15, [True] * 15, lambda: None,
                        m_cited=mask if masked_side != "source" else [False] * 15,
                        s_cited=mask if masked_side != "manuscript" else [False] * 15))
    assert not paths


def test_citation_inside_valid_match_is_diagnostic_not_scored():
    m = "a b c d (Smith 2020) e f g h i"
    s = "a b c d q e f g h i"
    result = compare(m, s)
    match = next(m for m in result["matches"] if m["match_kind"] == "similar")
    assert match["matched_words"] == 9
    assert 4 not in match["scored_word_positions"] and 5 not in match["scored_word_positions"]
    diagnostic = match["diagnostics"]
    assert diagnostic["matched_non_citation_words"] == 9
    assert diagnostic["citation_tokens_inside_span"] == {"manuscript": 2, "source": 0}
    assert diagnostic["citation_matches"]["count"] == 0
    assert diagnostic["manuscript_density"] == 9 / 11
    assert diagnostic["manuscript_gap_sequence"] == [0, 0, 0, 2, 0, 0, 0, 0]
    assert diagnostic["seed"]["words"] == ["a", "b", "c"]
    assert "manuscript_gap_sequence" in diagnostics_csv(result)


def test_exact_citation_exception_is_explicit_and_unchanged():
    text = "a b c d (Smith 2020) e f g h i"
    result = compare(text, text)
    assert result["metrics"]["exact_words"] == len(tokens(text))
    exact = next(m for m in result["matches"] if m["match_kind"] == "exact")
    assert exact["diagnostics"]["citation_matches"]["count"] == 2
    assert "applies to Similar" in exact["diagnostics"]["exact_citation_policy"]


def test_seven_prose_plus_two_citations_exact_accepts_similar_rejects():
    phrase = "one two three four (Smith 2020) five six seven"
    exact = compare(phrase, phrase)
    assert exact["metrics"]["exact_words"] == 9
    assert exact["metrics"]["overlapping_words"] == 9
    flexible = compare(phrase.replace("five", "x five"), phrase.replace("five", "y five"))
    assert flexible["metrics"]["overlapping_words"] == 0


def test_raw_citation_equal_pairs_never_shorten_qualifying_gaps():
    m = "a b c d (Smith 2020) x e f g h i"
    s = "a b c d (Smith 2020) y e f g h i"
    result = compare(m, s)
    match = next(item for item in result["matches"] if item["match_kind"] == "similar")
    assert len(match["qualifying_aligned_pairs"]) == 9
    assert len(match["raw_aligned_pairs"]) == 11
    assert match["matched_words"] == match["diagnostics"]["matched_word_count"] == 9
    assert match["diagnostics"]["citation_matches"]["count"] == 2
    assert match["diagnostics"]["maximum_gap"] == 3
    assert match["diagnostics"]["manuscript_density"] == 9 / 12
    assert match["scored_word_positions"] == [0, 1, 2, 3, 7, 8, 9, 10, 11]


def test_small_exhaustive_citation_oracle_keeps_alternative_legal_paths():
    from test_improved_eng import oracle
    for suffix in itertools.product("xy", repeat=3):
        m = list("abcdefghi") + list(suffix)
        s = list("abcdefghi") + list(reversed(suffix))
        for citation_index in (None, 5, 10):
            mc = [i == citation_index for i in range(len(m))]
            sc = [False] * len(s)
            # Unique sentinels remove equal citation nodes without compacting coordinates.
            reference_m = ["CITATION_SENTINEL" if mc[i] else w for i, w in enumerate(m)]
            expected = oracle(reference_m, s)
            actual = list(search(m, s, [True] * len(m), [True] * len(s), lambda: None,
                                 m_cited=mc, s_cited=sc))
            assert all(p in expected for p in actual)
            assert all(any(set(p) <= set(q) for q in actual) for p in expected)


def test_tight_materialization_budget_keeps_partial_report_not_key_error(monkeypatch):
    add = engine._Evidence.add

    def fail(self, size):
        if size >= 10240:
            raise engine.SearchLimit("evidence-memory-limit", "Synthetic materialization boundary.")
        return add(self, size)

    monkeypatch.setattr(engine._Evidence, "add", fail)
    result = compare("a b c d x e f g h i", "a b c d y e f g h i")
    assert result["metrics"]["truncated"]
    assert not result["matches"]
    assert result["source_coverage"][0]["status"] == "compared-with-limits"
