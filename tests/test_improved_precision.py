"""Original synthetic policy fixtures, not a private-paper calibration corpus."""
import pytest

from buna.citation_tokens import improved_citation_mask
from buna.classified import tokens
from buna.documents import _structure
from buna.improved_eng import improved_report
from buna.improved_layout import running_headers


def doc(*pages):
    return _structure([{"number": i + 1, "text": page} for i, page in enumerate(pages)], "Synthetic", [])


def compare(target, source, **kwargs):
    return improved_report(target, [{"id": "s", "document": source}], manuscript_scope="whole-document", **kwargs)


def test_numbered_headers_removed_without_changing_original_ledger_or_highlights():
    target = doc("METHODS AND MEASUREMENTS 1\nalpha beta gamma delta epsilon\nzeta eta",
                 "METHODS AND MEASUREMENTS 2\ntheta iota kappa lambda mu\nnu xi")
    source = doc("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi ending")
    before = target["text"]
    result = compare(target, source)
    assert target["text"] == before
    assert result["metrics"]["header_removed_words"] == 8
    assert result["metrics"]["score_denominator_words"] == 14
    assert result["metrics"]["exact_words"] == 14
    match = result["matches"][0]
    assert match["manuscript_span"] == 14
    for a, b in match["manuscript"]["equal_spans"]:
        assert "METHODS" not in before[a:b]
    assert match["aligned_pairs"][7][0] == 15
    assert match["diagnostics"]["header_removed_words"] == 4


def test_source_headers_and_odd_even_identity():
    source = doc(*[f"{'EVEN MEASUREMENT' if i % 2 else 'ODD MEASUREMENT'} {i + 1}\n"
                   f"body words page\nlast body line" for i in range(4)])
    mask, warnings = running_headers(source, tokens(source["text"]))
    assert sum(mask) == 12
    assert warnings  # Repeated unnumbered bottom body lines are retained.
    target = doc("body words page last body line body words page last body line extra")
    result = compare(target, source)
    assert result["metrics"]["exact_words"] == 12
    assert all("MEASUREMENT" not in source["text"][a:b] for match in result["matches"]
               for a, b in match["source"]["equal_spans"])


def test_ambiguous_unnumbered_prose_headings_and_first_page_title_retained():
    target = doc("Repeated prose here\nbody second line\nlast body line",
                 "Repeated prose here\nbody second line\nlast body line")
    mask, warnings = running_headers(target, tokens(target["text"]))
    assert not any(mask)
    assert warnings
    target = doc("STUDY METHODS\nbody second line\nlast body line",
                 "STUDY METHODS 2\nbody second line\nlast body line",
                 "STUDY METHODS 3\nbody second line\nlast body line")
    mask, _ = running_headers(target, tokens(target["text"]))
    assert mask[:2] == [False, False]
    assert sum(mask) == 6


@pytest.mark.parametrize("citation", [
    "Moll et al., 2005; Sambataro et al., 2006",
    "(Smith & Jones, 2020a;\nBrown et al., 2021)",
    "Smith (2005)", "Smith et al. (2005)", "Smith and Jones (2005)",
    "(J. R. Smith & K. Jones, 2005)", "(Smith, J. R., & Jones, K., 2005)",
    "(Smith 2005)", "(Smith, 2005a, b)", "[1, 2-4]",
])
def test_complete_apa_author_spans(citation):
    ledger = tokens(citation)
    assert all(improved_citation_mask(citation, ledger))


@pytest.mark.parametrize("text", [
    "the study began in 2005", "(participants recruited in 2005)",
    "Study (2005)", "ANOVA (2005)", "(n = 2005)", "(working memory)",
    "et al., 2005", "(2020)", "Results, 2005",
])
def test_non_citation_years_statistics_and_fragments(text):
    assert not any(improved_citation_mask(text, tokens(text)))


def test_inline_citation_mismatch_does_not_seed_similar():
    target = doc("Moll et al., 2005; Sambataro et al., 2006 alpha beta gamma delta")
    source = doc("Bell et al., 2005; Sommer et al., 2006 alpha beta gamma other")
    assert not compare(target, source)["matches"]


def test_exact_seven_prose_two_citation_exception():
    phrase = "alpha beta gamma delta (Smith 2005) epsilon zeta eta"
    result = compare(doc(phrase), doc(phrase + " ending"))
    assert result["metrics"]["exact_words"] == 9
    assert result["matches"][0]["diagnostics"]["citation_matches"]["count"] == 2
