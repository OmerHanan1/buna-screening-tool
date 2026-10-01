"""Synthetic staged operator/citation/edge fixtures; no private-paper text."""
import pytest
import itertools

from buna.classified import tokens as old_tokens
from buna.documents import _structure
from buna.improved_eng import _content, improved_report
from buna.improved_tokens import tokens
from buna.citation_tokens import apa_units, improved_citation_mask
from buna.improved_eng import _CitationBlocks, _Evidence, _collect, search, trim_weak_edges


def doc(text):
    return _structure([{"number": 1, "text": text}], "Synthetic", [])


def compare(target, source):
    return improved_report(doc(target), [{"id": "s", "document": doc(source + " ending")}],
                           manuscript_scope="whole-document")


def test_operators_are_literal_units_with_original_offsets_not_meaningful():
    text = "M=0.06 SD < 0.71 CI>1 p≈0.05 <= >= == \u2264 \u2265 !="
    ledger = tokens(text)
    assert [word for word, *_ in ledger] == [
        "m", "=", "0.06", "sd", "<", "0.71", "ci", ">", "1", "p", "\u2248", "0.05",
        "<", "=", ">", "=", "=", "=", "=",
    ]
    assert all(text[a:b].casefold() == word for word, a, b in ledger)
    assert all(not _content(op) for op in "=<>\u2248")
    assert [word for word, *_ in old_tokens(text)] == ["m", "0.06", "sd", "0.71", "ci", "1", "p", "0.05"]


def test_operator_addition_crosses_nine_unit_boundary_without_number_wildcards():
    target = "(M = 0.06, SD = 0.71) than with abstract construal"
    source = "(M = 0.08, SD = 0.90) than with abstract construal"
    result = compare(target, source)
    assert result["metrics"]["total_words"] == 10
    assert result["metrics"]["operator_words"] == 2
    assert not result["matches"]  # Eight, not nine, actual equal units.
    result = compare(target + " conditions", source + " conditions")
    assert result["metrics"]["similar_only_words"] == 9
    assert result["metrics"]["score_denominator_words"] == 11
    match = result["matches"][0]
    assert 2 not in match["scored_word_positions"] and 5 not in match["scored_word_positions"]
    assert match["matched_words"] == 9


def test_operator_pdf_glyphs_highlight_without_different_numeric_neighbors(tmp_path):
    import hashlib
    import pymupdf
    from buna.documents import extract_document
    from buna.pdf_reports import generate_pdf
    phrase = "(M=0.06, SD=0.71) than with abstract construal conditions"
    original, output = tmp_path / "operators.pdf", tmp_path / "report.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((60, 90), phrase, fontsize=10)
        pdf.save(original)
    target = extract_document(original)
    result = improved_report(target, [{"id": "s", "document": doc(
        phrase.replace("0.06", "0.08").replace("0.71", "0.90"))}], manuscript_scope="whole-document")
    result["papers"] = [{"id": "s", "source_number": 1, "title": "Synthetic", "status": "compared"}]
    mapping = generate_pdf({"original": str(original), "report": result, "job": {
        "filename": "operators.pdf", "document": target,
        "manuscript_sha256": hashlib.sha256(original.read_bytes()).hexdigest()}}, output)
    assert mapping["unmapped_regions"] == 0
    with pymupdf.open(output) as pdf:
        page = pdf[mapping["summary_pages"]]
        rectangles = []
        for annotation in page.annots() or []:
            if annotation.type[1] == "Highlight":
                vertices = annotation.vertices
                rectangles.extend(pymupdf.Quad(vertices[i:i + 4]).rect for i in range(0, len(vertices), 4))
        glyphs = [char for block in page.get_text("rawdict")["blocks"] for line in block.get("lines", [])
                  for span in line["spans"] for char in span["chars"]]
        for glyph in glyphs:
            if glyph["c"] == "=" or glyph["c"].isdigit():
                center = pymupdf.Rect(glyph["bbox"]).tl + pymupdf.Rect(glyph["bbox"]).br
                center /= 2
                assert any(rect.contains(center) for rect in rectangles) == (glyph["c"] == "=")


@pytest.mark.parametrize("operator", list("=<>\u2248"))
def test_exact_operators_count_in_both_sides_and_denominator(operator):
    phrase = f"alpha beta gamma {operator} delta epsilon zeta eta theta"
    result = compare(phrase, phrase)
    assert result["metrics"]["exact_words"] == result["metrics"]["eligible_words"] == 9
    assert result["matches"][0]["source"]["text"] == phrase
    changed = phrase.replace(operator, {"=": "<", "<": ">", ">": "\u2248", "\u2248": "="}[operator])
    assert not compare(phrase, changed)["matches"]


@pytest.mark.parametrize("citation", [
    "(Gan et al., 2024)", "(J. R. Smith & K. Jones, 2005a)", "Smith et al. (2005)",
    "(Smith, J. R., & Jones, K., 2005)", "(van Dijk & de la Cruz, 2005)",
])
def test_complete_citation_unit_can_supply_nine_but_not_meaningful_words(citation):
    target = f"alpha beta gamma {citation} delta x epsilon"
    source = f"alpha beta gamma {citation} delta y epsilon"
    result = compare(target, source)
    match = next(m for m in result["matches"] if m["match_kind"] == "similar")
    expected = len(tokens(citation))
    assert match["diagnostics"]["verified_citation_words"] == expected
    assert match["diagnostics"]["qualifying_prose_words"] == 5
    assert match["diagnostics"]["distinct_matched_content_words"] == 5
    assert match["matched_words"] == 5 + expected
    assert match["diagnostics"]["seed"]["words"] == ["alpha", "beta", "gamma"]
    assert match["diagnostics"]["manuscript_density"] == (5 + expected) / (6 + expected)


@pytest.mark.parametrize("different", ["Bell et al., 2005", "Moll et al., 2006", "Moll et al., 2005b"])
def test_partial_citation_authors_or_years_cannot_qualify(different):
    target = "alpha beta gamma (Moll et al., 2005) delta x epsilon"
    source = f"alpha beta gamma ({different}) delta y epsilon"
    assert not compare(target, source)["matches"]


def test_complete_identical_unit_inside_different_lists_qualifies_independently():
    target = "alpha beta gamma (Gan et al., 2024; Bell et al., 2005) delta x epsilon"
    source = "alpha beta gamma (Gan et al., 2024; Moll et al., 2005) delta y epsilon"
    result = compare(target, source)
    match = next(m for m in result["matches"] if m["match_kind"] == "similar")
    assert match["diagnostics"]["verified_citation_words"] == 4
    assert match["matched_words"] == 9
    assert match["diagnostics"]["maximum_gap"] == 4
    assert all(a not in range(7, 11) for a, b in match["qualifying_aligned_pairs"])


def test_identical_units_do_not_enable_citation_only_similar_seeds():
    target = "(Gan et al., 2024) alpha x beta y gamma z delta w epsilon"
    source = "(Gan et al., 2024) alpha q beta r gamma s delta t epsilon"
    assert not compare(target, source)["matches"]
    exact = "(Gan et al., 2024; Luo et al., 2013; Smith 2005)"
    assert compare(exact, exact)["metrics"]["exact_words"] == len(tokens(exact))


def test_apa_units_cannot_stitch_across_headers_or_long_quotes():
    text = "Smith REMOVED HEADER et al. (2005)"
    ledger = tokens(text)
    assert not apa_units(text, ledger, [i in (1, 2) for i in range(len(ledger))])
    target = 'alpha beta gamma "Gan et al., 2024" delta x epsilon'
    source = 'alpha beta gamma "Gan et al., 2024" delta y epsilon'
    assert not compare(target, source)["matches"]


def test_atomic_citation_paths_never_emit_a_partial_unit():
    target = "alpha beta gamma (Gan et al., 2024) delta x epsilon zeta eta"
    source = "alpha beta gamma (Gan et al., 2024) delta y epsilon zeta eta"
    mt, st = tokens(target), tokens(source)
    mw, sw = [t[0] for t in mt], [t[0] for t in st]
    mc, sc = improved_citation_mask(target, mt), improved_citation_mask(source, st)
    blocks = _CitationBlocks(mw, sw, apa_units(target, mt), apa_units(source, st),
                             [True] * len(mw), [True] * len(sw), None, None)
    for path in search(mw, sw, [True] * len(mw), [True] * len(sw), lambda: None,
                       m_cited=mc, s_cited=sc, citation_blocks=blocks):
        assert {p for p in path if mc[p[0]] or sc[p[1]]} == blocks.verified_pairs(path)


def test_atomic_units_against_independent_exhaustive_pair_oracle():
    from test_improved_eng import oracle
    for left, right in itertools.product(("eta theta", "eta eta", "theta eta"), repeat=2):
        target = "alpha beta gamma (Smith 2005) delta epsilon " + left
        source = "alpha beta gamma (Smith 2005) delta epsilon " + right
        mt, st = tokens(target), tokens(source)
        mw, sw = [t[0] for t in mt], [t[0] for t in st]
        mc, sc = improved_citation_mask(target, mt), improved_citation_mask(source, st)
        blocks = _CitationBlocks(mw, sw, apa_units(target, mt), apa_units(source, st),
                                 [True] * len(mw), [True] * len(sw), None, None)
        expected = []
        unit_pairs = {(3, 3), (4, 4)}
        for path in oracle(mw, sw):
            citation_pairs = {p for p in path if mc[p[0]] or sc[p[1]]}
            if citation_pairs and citation_pairs != unit_pairs:
                continue
            prose_seed = any(
                path[k:k + 3] == tuple((path[k][0] + offset, path[k][1] + offset) for offset in range(3))
                and all(not mc[a] and not sc[b] for a, b in path[k:k + 3])
                for k in range(len(path) - 2))
            if prose_seed:
                expected.append(path)
        actual = list(search(mw, sw, [True] * len(mw), [True] * len(sw), lambda: None,
                             m_cited=mc, s_cited=sc, citation_blocks=blocks))
        assert all(path in expected for path in actual)
        assert {pair for path in actual for pair in path} == {pair for path in expected for pair in path}


def test_full_multi_author_unit_cannot_be_replaced_by_equal_suffix():
    target = "alpha beta gamma (Moll, Bell, and Smith, 2005) delta x epsilon"
    source = "alpha beta gamma (Other, Bell, and Smith, 2005) delta y epsilon"
    assert not compare(target, source)["matches"]
    text = "(e.g., Feinberg et al., 2014; Olatunji et al., 2017)"
    ledger = tokens(text)
    units = apa_units(text, ledger)
    assert [[w for w, *_ in ledger[a:b]] for a, b in units] == [
        ["feinberg", "et", "al", "2014"], ["olatunji", "et", "al", "2017"],
    ]


@pytest.mark.parametrize("side", ["target", "source"])
@pytest.mark.parametrize("edge", ["prefix", "suffix"])
@pytest.mark.parametrize("fringe", ["the", "of the", "=", "< ="])
def test_weak_one_two_unit_padding_removed_before_nine_word_acceptance(side, edge, fringe):
    core = "alpha beta gamma delta epsilon zeta eta theta"
    plain = f"{fringe} {core}" if edge == "prefix" else f"{core} {fringe}"
    gapped = f"{fringe} borrowed several unrelated words {core}" if edge == "prefix" else f"{core} borrowed several unrelated words {fringe}"
    target, source = (gapped, plain) if side == "target" else (plain, gapped)
    result = compare(target, source)
    assert result["metrics"]["overlapping_words"] == 0
    assert result["source_coverage"][0]["trimmed_edge_pair_occurrences"] >= len(tokens(fringe))


@pytest.mark.parametrize("fringe", ["neural", "neural response", "0.05", "p", "the of and"])
def test_meaningful_edges_and_three_stopword_run_not_trimmed(fringe):
    core = "alpha beta gamma delta epsilon zeta eta theta"
    result = compare(f"{fringe} {core}", f"{fringe} borrowed several unrelated words {core}")
    assert result["metrics"]["overlapping_words"] == 8 + len(tokens(fringe))
    assert result["source_coverage"][0]["trimmed_edge_pair_occurrences"] == 0


def test_internal_weak_words_and_ordinary_gaps_preserved():
    target = "alpha beta gamma x the y delta epsilon zeta eta theta"
    source = "alpha beta gamma q the r delta epsilon zeta eta theta"
    result = compare(target, source)
    assert result["metrics"]["overlapping_words"] == 9
    assert 4 in result["matches"][0]["scored_word_positions"]
    assert result["matches"][0]["max_unmatched_run"] == 1


def test_contiguous_exact_stopword_edges_unchanged_and_merged_padding_not_restored():
    phrase = "the alpha beta gamma delta epsilon zeta eta theta of"
    assert compare(phrase, phrase)["metrics"]["exact_words"] == 10
    words = phrase.split() + ["iota", "kappa", "lambda"]
    full = ((0, 0),) + tuple((i, i + 3) for i in range(1, len(words)))
    first, second = full[:11], full[3:]
    budget, paths = _Evidence(), []
    for candidate in (first, second, first):
        _collect(paths, candidate, budget, lambda: None,
                 prepare=lambda p: trim_weak_edges(p, words))
    assert all((0, 0) not in p for p in paths)
    assert {pair for p in paths for pair in p} == set(full[1:])
