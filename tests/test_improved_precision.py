"""Original synthetic policy fixtures, not a private-paper calibration corpus."""
import pytest
import itertools

from buna.citation_tokens import improved_citation_mask
from buna.classified import tokens
from buna.documents import _structure
from buna.improved_eng import improved_report
from buna.improved_eng import _content, accepts_similar, content_details
from buna.improved_eng import _Evidence, _collect, _path_bytes
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
    "(Smith 2005)", "(Smith, 2005a, b)", "Smith (2005, 2006b)",
    "(van Dijk & de la Cruz, 2005)", "[1, 2-4]",
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


@pytest.mark.parametrize("name", ["'toolkit'", '"toolkit"', "\u2018toolkit\u2019", "\u201c'toolkit'\u201d",
                                 '"the toolkit package"'])
def test_short_technical_quotes_do_not_exclude_match(name):
    phrase = f"estimated marginal means were computed using the {name} package today"
    result = compare(doc(phrase), doc(phrase + " ending"))
    assert result["metrics"]["overlapping_words"] == len(tokens(phrase))
    assert not result["metrics"]["excluded_quotation_words"]
    assert result["matches"][0]["quotation"]["matched_quoted_words"] == 0


@pytest.mark.parametrize("side", ["target", "source", "both"])
@pytest.mark.parametrize("count,accepted", [(4, True), (5, True), (6, False)])
def test_long_quotes_are_individual_gap_tokens(side, count, accepted):
    first, last = "alpha beta gamma delta epsilon", "zeta eta theta iota kappa"
    quote = '"' + " ".join(["quoted"] * count) + '"'
    target = first + " " + (quote if side != "source" else "") + " " + last
    source = first + " " + (quote if side != "target" else "") + " " + last + " ending"
    result = compare(doc(target), doc(source))
    assert result["metrics"]["overlapping_words"] == (10 if accepted else 0)
    assert result["metrics"]["eligible_words"] == 10
    if accepted:
        match = result["matches"][0]
        assert match["match_kind"] == "similar"
        assert match["max_unmatched_run"] == count
        assert match["similarity"] == 10 / (10 + count)
        assert match["quotation"]["matched_quoted_words"] == 0
        assert not match["exclusion_reasons"]


def test_eight_eligible_words_cannot_be_rescued_by_long_quote():
    phrase = 'alpha beta gamma delta "long quoted material here" epsilon zeta eta theta'
    result = compare(doc(phrase), doc(phrase + " ending"))
    assert result["metrics"]["eligible_words"] == 8
    assert result["metrics"]["overlapping_words"] == 0
    assert compare(doc(phrase), doc(phrase + " ending"), exclude_quotes=False)["metrics"]["exact_words"] == 12


def test_quote_outside_aligned_match_has_no_exclusion_effect():
    phrase = "alpha beta gamma delta epsilon zeta eta theta iota"
    target = doc(phrase + ' "this long quotation is outside"')
    result = compare(target, doc(phrase + " different ending"))
    assert result["metrics"]["exact_words"] == 9
    assert result["matches"][0]["quotation"]["status"] == "not-detected"
    assert not result["matches"][0]["exclusion_reasons"]


@pytest.mark.parametrize("word,expected", [
    ("the", False), ("et", False), ("f", True), ("df", True), ("sd", True),
    ("η", True), ("4.72", True), ("2020a", False), ("construal", True),
    ("p", True), ("d", True), ("ci", True), ("m", True), ("0.05", True),
    ("2005", True), ("4,72", True), ("x", False),
    ("level", True), ("levels", True), ("rna", True), ("ai", True),
])
def test_frozen_literal_content_definition(word, expected):
    assert _content(word) is expected


def test_similar_requires_four_distinct_shared_content_words():
    target = doc("the construal level of the reappraisal they implement to the construal level of the")
    source = doc("the construal level of the explanation they apply to the construal level of the")
    assert compare(target, source)["metrics"]["overlapping_words"] == 0
    # Nine uninterrupted generic/statistical words remain valid Exact.
    phrase = "the of and the of and f p 2020"
    assert compare(doc(phrase), doc(phrase + " ending"))["metrics"]["exact_words"] == 9


def test_three_word_anchor_needs_one_meaningful_word():
    words = "the neural response was measured across distinct cortical regions".split()
    path = tuple((i, i + int(i >= 4) + int(i >= 7)) for i in range(9))
    assert accepts_similar(path, words, [False] * 9, [False] * 11)
    details = content_details(path, words, [False] * 9, [False] * 11)
    assert details["distinct_matched_content_words"] == 7
    assert details["strongest_three_word_run_meaningful_words"] == 3
    path = tuple((i, i + i // 3) for i in range(9))
    assert accepts_similar(path, words, [False] * 9, [False] * 12)
    path = tuple((i, i + i // 2) for i in range(9))
    assert not accepts_similar(path, words, [False] * 9, [False] * 14)


def test_citations_cannot_create_an_artificial_three_word_anchor():
    words = "alpha beta smith gamma delta epsilon zeta eta theta iota".split()
    path = tuple((i, i) for i in range(10))
    mask = [i == 2 for i in range(10)]
    info = content_details(path, words, mask, mask)
    assert info["longest_exact_run"] == 7
    assert info["distinct_matched_content_words"] == 9


def test_statistics_and_numbers_supply_meaningful_types_with_three_word_anchor():
    target = doc("the p was alpha d and 0.05 beta CI of 2.50")
    source = doc("the p was gamma d and 0.05 delta CI of 2.50")
    result = compare(target, source)
    match = result["matches"][0]
    assert result["metrics"]["exact_words"] == 0
    assert result["metrics"]["similar_only_words"] == 9
    assert match["diagnostics"]["distinct_matched_content_words"] == 5
    assert match["diagnostics"]["longest_exact_run"] == 3
    assert match["diagnostics"]["strongest_three_word_run_meaningful_words"] == 2
    assert match["diagnostics"]["content_policy_version"] == "literal-meaningful-types-numbers-statistics-v2"
    assert match["diagnostics"]["seed"]["words"] == ["the", "p", "was"]
    assert match["matched_words"] == 9


def test_exactly_one_meaningful_word_in_only_three_word_run_qualifies():
    words = "the p was d and ci of m to".split()
    path = tuple((i, j) for i, j in enumerate([0, 1, 2, 4, 5, 7, 8, 10, 11]))
    details = content_details(path, words, [False] * 9, [False] * 12)
    assert details["distinct_matched_content_words"] == 4
    assert details["strongest_three_word_run_meaningful_words"] == 1
    assert accepts_similar(path, words, [False] * 9, [False] * 12)
    # A three-word function-word seed is insufficient even with statistics later.
    words[:3] = ["the", "of", "and"]
    words[4] = "sd"
    assert not accepts_similar(path, words, [False] * 9, [False] * 12)


@pytest.mark.parametrize("phrase", ["the of and of the and the of and", "the p was d and p of d to"])
def test_stopwords_or_fewer_than_four_meaningful_types_still_reject(phrase):
    words = phrase.split()
    path = tuple((i, i + int(i >= 3)) for i in range(9))
    assert not accepts_similar(path, words, [False] * 9, [False] * 10)


def test_different_numeric_literals_never_match_or_become_distinct_credit():
    result = compare(doc("the p was alpha d and 0.05 beta CI of 2.50"),
                     doc("the p was gamma d and 0.06 delta CI of 2.51"))
    assert not result["matches"]  # Only seven actual equal words, never nine.
    result = compare(doc("the p was alpha d and 0.05 beta CI of 0.05"),
                     doc("the p was gamma d and 0.05 delta CI of 0.05"))
    assert result["matches"][0]["diagnostics"]["distinct_matched_content_words"] == 4
    assert result["matches"][0]["diagnostics"]["matched_word_count"] == 9


@pytest.mark.parametrize("citation_side", ["target", "source", "both"])
def test_citation_years_never_gain_numeric_meaningful_credit(citation_side):
    cited = "(Smith 2005; Jones 2006) the p was alpha d and CI beta M of SD"
    ordinary = "Smith 2005 Jones 2006 the p was gamma d and CI delta M of SD"
    target = cited if citation_side != "source" else ordinary.replace("gamma", "alpha").replace("delta", "beta")
    source = cited.replace("alpha", "gamma").replace("beta", "delta") if citation_side != "target" else ordinary
    result = compare(doc(target), doc(source + " ending"))
    match = next(m for m in result["matches"] if m["match_kind"] == "similar")
    if citation_side != "both":
        assert all(a >= 4 and b >= 4 for a, b in match["aligned_pairs"])
    else:
        assert match["diagnostics"]["verified_citation_words"] == 4
    assert match["diagnostics"]["distinct_matched_content_words"] == 5
    assert match["matched_words"] == (13 if citation_side == "both" else 9)


def test_thirty_eight_duplicates_charge_one_retained_path():
    path = tuple((i, i) for i in range(12))
    budget, paths = _Evidence(), []
    for _ in range(38):
        _collect(paths, path, budget, lambda: None)
    assert paths == [path]
    assert budget.bytes == budget.peak_bytes == _path_bytes(path)


def test_transitive_overlap_union_is_order_independent_and_never_fills_gaps():
    full = tuple((i, i + i // 5) for i in range(23))
    candidates = [full[:12], full[6:18], full[11:]]
    for order in itertools.permutations(candidates):
        paths, budget = [], _Evidence()
        for path in order:
            _collect(paths, path, budget, lambda: None)
        assert paths == [full]
        assert budget.bytes == _path_bytes(full)


def test_incompatible_alternative_and_independent_occurrence_are_preserved():
    first = tuple((i, i + int(i >= 5)) for i in range(12))
    conflicting = first[:5] + ((5, 5),) + first[6:]
    independent = tuple((a, b + 100) for a, b in first)
    paths, budget = [], _Evidence()
    for path in (first, conflicting, independent):
        _collect(paths, path, budget, lambda: None)
    assert set(paths) == {first, conflicting, independent}
    assert budget.bytes == sum(_path_bytes(p) for p in paths)


def test_same_envelope_alternatives_materialize_once_without_losing_pairs(monkeypatch):
    from buna import improved_eng as engine
    words = "alpha beta gamma delta epsilon epsilon zeta eta theta iota kappa lambda"
    source = words.replace("epsilon epsilon", "epsilon spacer epsilon")
    first = tuple((i, i) for i in range(5)) + tuple((i, i + 1) for i in range(6, 12))
    second = tuple((i, i) for i in range(4)) + ((5, 4),) + first[5:]
    # The two actual paths use different equal epsilon target positions.
    def alternatives(*args, **kwargs):
        yield first
        yield second
        yield first
    monkeypatch.setattr(engine, "search", alternatives)
    result = compare(doc(words), doc(source + " ending"))
    similar = [match for match in result["matches"] if match["match_kind"] == "similar"]
    assert len(similar) == 1
    match = similar[0]
    assert match["alternative_alignments"]
    actual = set(map(tuple, match["aligned_pairs"]))
    for alternative in match["alternative_alignments"]:
        actual.update(map(tuple, alternative))
    assert actual == set(first) | set(second)
    assert match["scored_word_positions"] == sorted({a for a, _ in actual})


@pytest.mark.parametrize("number_first", [True, False])
def test_separate_header_counter_line(number_first):
    pages = []
    for page in (1, 2):
        header = f"{page}\nREPEATED RUNNING HEADER" if number_first else f"REPEATED RUNNING HEADER\n{page}"
        pages.append(header + "\nalpha beta gamma delta\nother body words")
    target = doc(*pages)
    mask, _ = running_headers(target, tokens(target["text"]))
    assert sum(mask) == 8


def test_real_pdf_header_glyphs_never_highlighted(tmp_path):
    import hashlib
    import pymupdf
    from buna.documents import extract_document
    from buna.pdf_reports import generate_pdf
    original, output = tmp_path / "original.pdf", tmp_path / "report.pdf"
    body = [("alpha beta gamma delta", "epsilon zeta eta"),
            ("theta iota kappa lambda", "mu nu xi")]
    with pymupdf.open() as pdf:
        for index, lines in enumerate(body, 1):
            page = pdf.new_page()
            page.insert_text((60, 40), f"REPEATED RUNNING HEADER {index}")
            page.insert_text((60, 100), lines[0])
            page.insert_text((60, 120), lines[1])
        pdf.save(original)
    target = extract_document(original)
    result = compare(target, doc(" ".join(line for lines in body for line in lines) + " ending"))
    assert result["metrics"]["exact_words"] == 14
    result["papers"] = [{"id": "s", "source_number": 1, "title": "Synthetic", "status": "compared"}]
    mapping = generate_pdf({"original": str(original), "report": result, "job": {
        "filename": "original.pdf", "document": target,
        "manuscript_sha256": hashlib.sha256(original.read_bytes()).hexdigest()}}, output)
    assert mapping["unmapped_regions"] == 0
    with pymupdf.open(output) as pdf:
        for page in list(pdf)[mapping["summary_pages"]:]:
            rectangles = []
            for annotation in page.annots() or []:
                if annotation.type[1] == "Highlight":
                    vertices = annotation.vertices
                    rectangles.extend(pymupdf.Quad(vertices[i:i + 4]).rect for i in range(0, len(vertices), 4))
            assert rectangles
            for x0, y0, x1, y1, word, *_ in page.get_text("words"):
                if x0 < 50:  # Native E/S source margin label, not a document word.
                    continue
                marked = any(rect.intersects(pymupdf.Rect(x0, y0, x1, y1)) for rect in rectangles)
                assert marked == (y0 > 60), word


def test_exclusion_union_and_source_order_are_stable():
    phrase = 'alpha beta gamma delta "quoted long material here" epsilon zeta eta theta iota'
    target = doc("REPEATED HEADER 1\ncover text\nAbstract\n" + phrase,
                 "REPEATED HEADER 2\nReferences\n" + phrase)
    source = doc(phrase + " ending")
    sources = [{"id": sid, "source_number": number, "document": source}
               for number, sid in enumerate(("a", "b"), 1)]
    first = improved_report(target, sources)
    second = improved_report(target, list(reversed(sources)))
    assert first["metrics"] == second["metrics"]
    assert first["matches"] == second["matches"]
    metrics = first["metrics"]
    assert metrics["score_denominator_words"] == 10  # Abstract plus nine prose.
    assert metrics["overlapping_words"] == 9
    assert sum(metrics[key] for key in ("front_matter_words", "excluded_bibliography_words",
                                       "excluded_quotation_words", "other_excluded_manuscript_words",
                                       "eligible_words")) == metrics["total_words"]


def test_repeated_occurrences_group_in_reader_without_losing_sources():
    from buna.presentation import manuscript_reader
    phrase = "alpha beta gamma delta epsilon zeta eta theta iota"
    sources = [{"id": sid, "source_number": number, "document": doc(
        phrase + " unrelated " * 6 + phrase)} for number, sid in enumerate(("a", "b"), 1)]
    result = improved_report(doc(phrase), sources, manuscript_scope="whole-document")
    result["papers"] = sources
    reader = manuscript_reader(result)
    assert len(result["matches"]) == 4  # Two true locations in each of two papers.
    assert len(reader["groups"]) == 1
    assert len(reader["groups"][0]["match_indices"]) == 4
    assert result["metrics"]["overlapping_words"] == 9


def test_reference_probe_uses_saved_header_and_quote_profile():
    from buna.span_evaluation import _reference_probe
    target = doc("REPEATED HEADER 1\nalpha beta gamma delta\nother prose",
                 'REPEATED HEADER 2\nepsilon zeta "four quoted words here"\neta theta iota')
    source = doc("alpha beta gamma delta other prose epsilon zeta eta theta iota ending")
    result = compare(target, source)
    eligible = {i for item in result["classification"]["coverage_intervals"]
                if not item["state"].startswith("excluded")
                for i in range(item["word_start"], item["word_end"])}
    probe = _reference_probe(result, {"source_id": "s", "word_start": 0,
                                     "word_end": len(tokens(target["text"])),
                                     "source_word_start": 0, "source_word_end": 11}, {"s": source}, eligible)
    assert probe["matched_non_citation_words"] == 11
    assert probe["maximum_gap"] == 4
    assert probe["contains_excluded_gap_tokens"]
    assert not probe["crosses_exclusion_boundary"]
    assert probe["seed"]["aligned_pairs"][0] == [3, 0]


def test_citation_and_short_quote_recognition_after_logical_header_removal():
    from buna.improved_eng import _long_quotes
    target = doc("REPEATED HEADER 1\nbody words\nSmith et",
                 "REPEATED HEADER 2\nal. (2005)\nlast body words")
    ledger = tokens(target["text"])
    headers, _ = running_headers(target, ledger)
    mask = improved_citation_mask(target["text"], ledger, headers)
    assert [word for (word, *_), cited in zip(ledger, mask) if cited] == ["smith", "et", "al", "2005"]
    target = doc('REPEATED HEADER 1\nbody words\n"one',
                 'REPEATED HEADER 2\ntwo three"\nlast body words')
    ledger = tokens(target["text"])
    headers, _ = running_headers(target, ledger)
    quoted, _ = _long_quotes(target["text"], ledger, headers)
    assert not any(quoted)


def test_compaction_preserves_exhaustive_accepted_pair_coverage():
    from test_improved_eng import oracle
    from buna.improved_eng import _is_exact
    prefix = "alpha beta gamma delta epsilon zeta".split()
    variants = [prefix + list(suffix) for suffix in itertools.product(("eta", "theta"), repeat=3)]
    for target, source in itertools.product(variants, repeat=2):
        expected = [path for path in oracle(target, source)
                    if _is_exact(path) or accepts_similar(path, target, [False] * 9, [False] * 9)]
        result = compare(doc(" ".join(target)), doc(" ".join(source) + " ending"))
        observed = set()
        for match in result["matches"]:
            for alignment in [match["aligned_pairs"], *match["alternative_alignments"]]:
                observed.update(map(tuple, alignment))
        assert observed == {pair for path in expected for pair in path}
        assert result["metrics"]["overlapping_words"] == len({a for a, _ in observed})
