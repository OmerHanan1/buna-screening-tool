import itertools
import random

import pytest

from buna import improved_eng as engine
from buna.comparison import ComparisonCancelled
from buna.documents import _structure
from buna.improved_eng import improved_report, measures, search, valid
from buna.result_state import result_state


def document(*pages):
    return _structure([{"number": i + 1, "text": text} for i, text in enumerate(pages)], "Synthetic", [])


def report(m, s, **kwargs):
    return improved_report(document(m), [{"id": "s", "document": document(s + " sourceending")}], **kwargs)


def found(m, s):
    return list(search(m.split(), s.split(), [True] * len(m.split()), [True] * len(s.split()), lambda: None))


def oracle(m, s):
    """Independent tiny exhaustive enumeration, including unsaturated paths."""
    pairs = [(i, j) for i, a in enumerate(m) for j, b in enumerate(s) if a == b]
    accepted = []

    def visit(path):
        if len(path) >= 9:
            first, last = path[0], path[-1]
            seeded = any(path[k + 1] == (path[k][0] + 1, path[k][1] + 1)
                         and path[k + 2] == (path[k][0] + 2, path[k][1] + 2) for k in range(len(path) - 2))
            if seeded and len(path) * 5 >= 3 * max(last[0] - first[0] + 1, last[1] - first[1] + 1):
                accepted.append(tuple(path))
        for q in pairs:
            if 1 <= q[0] - path[-1][0] <= 6 and 1 <= q[1] - path[-1][1] <= 6:
                visit(path + [q])

    for pair in pairs:
        visit([pair])
    return accepted


def test_exhaustive_binary_suffix_oracle_no_false_positives_or_missed_pairs():
    prefix = list("ABCDEF")
    variants = [prefix + list(suffix) for suffix in itertools.product("xy", repeat=4)]
    for m, s in itertools.product(variants, repeat=2):
        expected = oracle(m, s)
        actual = list(search(m, s, [True] * len(m), [True] * len(s), lambda: None))
        expected_set = set(expected)
        assert all(path in expected_set for path in actual)
        actual_sets = [set(path) for path in actual]
        assert all(any(set(path) <= candidate for candidate in actual_sets) for path in expected)


def test_seeded_random_edits_against_exhaustive_oracle():
    rng = random.Random(8461)
    for _ in range(100):
        m, s = list("abcdefghijk"), list("abcdefghijk")
        for words in (m, s):
            for _ in range(rng.randrange(4)):
                words.insert(rng.randrange(len(words)), rng.choice("axyk"))
        expected = oracle(m, s)
        actual = list(search(m, s, [True] * len(m), [True] * len(s), lambda: None))
        assert all(p in expected for p in actual)
        assert all(any(set(p) <= set(q) for q in actual) for p in expected)


def test_seed_and_minimum():
    for size in (3, 8, 9):
        phrase = " ".join(f"word{i}" for i in range(size))
        result = report(phrase, phrase)
        assert result["metrics"]["overlapping_words"] == (9 if size == 9 else 0)
        assert result["metrics"]["score_denominator_words"] == size


def test_restored_fragmented_islands_qualify_without_content_anchors():
    m = "a b c d x x e f g y h i j"
    s = "a b c d q e f g r h i j"
    result = improved_report(document(m), [
        {"id": "z", "document": document(s)}, {"id": "a", "document": document(s + " ending")},
    ])
    assert result["metrics"]["overlapping_words"] == 10
    assert result["metrics"]["eligible_words"] == 13
    assert result["metrics"]["exact_words"] == 0
    assert result["matches"]
    assert all(row["overlapping_words"] == 10 for row in result["source_coverage"])


def test_exact_subrun_precedence_inside_larger_similar_passage():
    result = report("red blue green black pink gold silver gray white x tan rust teal",
                    "red blue green black pink gold silver gray white y tan rust teal")
    assert result["metrics"]["exact_words"] == 9
    assert result["metrics"]["similar_only_words"] == 3
    assert result["metrics"]["overlapping_words"] == 12
    assert {match["match_kind"] for match in result["matches"]} == {"exact", "similar"}


def test_repeated_seed_stress_has_explicit_partial_evidence():
    result = report("word " * 500 + "ending", "word " * 600 + "other", source_seconds=0.03)
    row = result["source_coverage"][0]
    assert row["status"] == "compared-with-limits"
    assert row["limits_reached"] == ["source-time-limit"]
    assert result["metrics"]["overlapping_words"] > 0
    assert result["metrics"]["not_fully_checked_words"] >= 1


@pytest.mark.parametrize("side", ["manuscript", "source"])
@pytest.mark.parametrize("gap", [5, 6])
def test_gap_boundary_each_side(side, gap):
    plain = "a b c d e f g h i j"
    interrupted = "a b c d e " + " ".join(["x"] * gap) + " f g h i j"
    m, s = (interrupted, plain) if side == "manuscript" else (plain, interrupted)
    assert bool(found(m, s)) is (gap == 5)


@pytest.mark.parametrize("side", ["manuscript", "source"])
@pytest.mark.parametrize("extra", [0, 1])
def test_density_boundary_each_side(side, extra):
    plain = "a b c d e f g h i"
    interrupted = "a b c x x x " + ("x " * extra) + "d e f y y y g h i"
    m, s = (interrupted, plain) if side == "manuscript" else (plain, interrupted)
    paths = found(m, s)
    assert bool(paths) is (extra == 0)
    if paths:
        assert measures(paths[0])[f"{side}_similarity"] == 0.60


def test_gap_is_not_combined():
    paths = found("a b c x x d e f g h i", "a b c y y y d e f g h i")
    assert paths and measures(paths[0])["max_unmatched_run"] == 3


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("count", [59, 60])
def test_exact_59_and_60_percent(side, count):
    positions = [0, 1, 2]
    for i in range(count - 3):
        positions.append(positions[-1] + (2 if i < 100 - count else 1))
    path = tuple((positions[i], i) if side == 0 else (i, positions[i]) for i in range(count))
    assert positions[-1] == 99
    assert valid(path) is (count == 60)


def test_failed_extension_retains_inner_and_long_gap_splits():
    m = "a b c d e f g h i j " + "x " * 6 + "k l m n o p q r s t"
    s = "a b c d e f g h i j k l m n o p q r s t"
    paths = found(m, s)
    assert sorted(len(p) for p in paths) == [10, 10]
    path = tuple((i * 2, i * 2) for i in range(8)) + tuple((16 + i, 16 + i) for i in range(9))
    windows = list(engine._windows(path, lambda: None))
    assert windows and all(valid(p) for p in windows)
    assert any(set(path[-9:]) <= set(p) for p in windows)


def test_density_recovery_and_accumulated_drift():
    path = tuple((i + 5 * (i // 9), i) for i in range(36))
    m = ["noise"] * (path[-1][0] + 1)
    s = [f"word{i}" for i in range(36)]
    for i, j in path:
        m[i] = s[j]
    result = list(search(m, s, [True] * len(m), [True] * len(s), lambda: None))
    assert path in result
    assert path[-1][0] - path[-1][1] == 15


def test_repeated_words_alternatives_and_reversal():
    m = "a b c d a e f g h i j"
    s = "a b c a d e f g h i j"
    expected = oracle(m.split(), s.split())
    paths = found(m, s)
    assert paths
    assert all(any(set(p) <= set(q) for q in paths) for p in expected)
    for p in paths:
        assert len({a for a, _ in p}) == len({b for _, b in p}) == len(p)
    assert not found("a b c d e f g h i", "g h i d e f a b c")


def test_occurrences_remain_distinct_and_compatible_runs_consolidate():
    phrase = "a b c d e f g h i"
    paths = found(phrase, phrase + " separator " * 7 + phrase)
    assert len(paths) == 2
    assert sorted(p[0][1] for p in paths) == [0, 16]
    assert found(phrase + " j k l", phrase + " j k l") == [tuple((i, i) for i in range(12))]


def test_hard_exclusion_boundaries_and_raw_audit():
    result = report('a b c d "quoted bridge here" e f g h i', "a b c d quoted bridge here e f g h i")
    assert result["metrics"]["overlapping_words"] == 0
    assert result["metrics"]["eligible_words"] == 9
    assert result["improved_eng"]["raw_excluded_evidence"]
    assert all(m["excluded_from_score"] and not m["scored_word_positions"]
               for m in result["improved_eng"]["raw_excluded_evidence"])
    assert not result["matches"]
    assert report('a b c d "quoted bridge here" e f g h i', "a b c d quoted bridge here e f g h i",
                  exclude_quotes=False)["metrics"]["overlapping_words"] == 12


def test_scope_bibliography_short_match_zero_and_missing_sources():
    phrase = "a b c d e f g h i"
    result = report("cover words\n\nAbstract\n" + phrase + "\n\nReferences\n" + phrase, phrase)
    assert result["metrics"]["overlapping_words"] == 9
    assert result["metrics"]["front_matter_words"] == 2
    assert result["metrics"]["eligible_words"] == 10
    empty = report('"' + phrase + '"', phrase)
    assert empty["metrics"]["overlap_percent"] is None
    assert result_state(empty)["state"] == "unscorable"
    missing = improved_report(document(phrase), [{"id": "missing"}, {"id": "excluded", "excluded": True}])
    assert missing["metrics"]["eligible_words"] == 9
    assert result_state(missing)["state"] == "not_compared"
    assert missing["source_coverage"][0]["preprint_status"] == "unknown"


def test_unicode_numbers_offsets_and_source_order():
    m = "Cafe\u0301 alpha beta gamma delta epsilon zeta eta theta 4.72 participants"
    s = "CAFÉ alpha beta gamma delta epsilon zeta eta theta 4.16 participants"
    result = report(m, s)
    assert result["metrics"]["overlapping_words"] == 10
    assert 9 not in {p for item in result["matches"] for p in item["scored_word_positions"]}
    for match in result["matches"]:
        for a, b in match["manuscript"]["equal_spans"]:
            assert "4.72" not in m[a:b]
    sources = [{"id": "a", "document": document(s)}, {"id": "b", "document": document("prefix " + s)}]
    assert improved_report(document(m), sources) == improved_report(document(m), list(reversed([
        {**sources[0], "source_number": 1}, {**sources[1], "source_number": 2}])))


def test_literal_punctuation_and_document_local_hyphen_ledger():
    text = "alpha beta gamma delta epsilon zeta eta theta iota"
    result = report(text.replace("alpha beta", "ALPHA—beta"), text)
    assert result["metrics"]["overlapping_words"] == 9
    hyphen = engine.tokens("inter-\nnational international", engine._line_join_words(["inter-\nnational international"]))
    assert hyphen == [("international", 0, 15), ("international", 16, 29)]


def test_limits_keep_real_exact_evidence_and_mark_unchecked(monkeypatch):
    monkeypatch.setattr(engine, "WORKING_INDEX_BYTES", 1)
    phrase = "red blue green black pink gold silver gray white tan"
    result = report(phrase + " unmatched", phrase)
    assert result["metrics"]["overlapping_words"] == 10
    assert result["metrics"]["truncated"]
    assert result["source_coverage"][0]["status"] == "compared-with-limits"
    assert result["classification"]["metrics"]["not_fully_checked_words"] == 1
    assert result_state(result)["score_kind"] == "lower_bound"


def test_evidence_limit_and_cancellation(monkeypatch):
    monkeypatch.setattr(engine, "MAX_EVIDENCE_BYTES", 1)
    result = report("a b c d e f g h i", "a b c d e f g h i")
    assert result["metrics"]["truncated"]
    assert result["source_coverage"][0]["limits_reached"] == ["evidence-memory-limit"]
    with pytest.raises(ComparisonCancelled):
        report("a b c", "a b c", cancelled=lambda: True)


def test_cancellation_inside_repetitive_graph():
    calls = 0

    def check():
        nonlocal calls
        calls += 1
        if calls == 100:
            raise ComparisonCancelled("cancelled in graph")

    with pytest.raises(ComparisonCancelled):
        list(search(["x"] * 30, ["x"] * 30, [True] * 30, [True] * 30, check))
    assert calls == 100


def test_audit_limit_is_not_scored_search_failure(monkeypatch):
    original = engine.search

    def audit_limit(mw, sw, m_ok, s_ok, *args, **kwargs):
        if all(m_ok):
            raise engine.SearchLimit("source-time-limit", "Audit budget ended.")
        yield from original(mw, sw, m_ok, s_ok, *args, **kwargs)

    monkeypatch.setattr(engine, "search", audit_limit)
    result = report('a b c d e f g h i "excluded quote"', "a b c d e f g h i")
    assert result["metrics"]["overlapping_words"] == 9
    assert result["source_coverage"][0]["scored_search_complete"]
    assert not result["source_coverage"][0]["audit_search_complete"]
    assert not result["improved_eng"]["audit_complete"]
    assert not result["metrics"]["truncated"]
    assert any("audit incomplete" in warning for warning in result["warnings"])


def test_source_limit_and_unvisited_source_are_explicit(monkeypatch):
    now = 0

    def clock():
        nonlocal now
        now += 0.1
        return now

    monkeypatch.setattr(engine.time, "monotonic", clock)
    result = improved_report(document("a b c d e f g h i"), [
        {"id": "a", "document": document("a b c d e f g h i ending")},
        {"id": "b", "document": document("another source")},
    ], total_time_limit_seconds=0.4)
    assert result["source_coverage"][0]["status"] == "compared-with-limits"
    assert result["source_coverage"][1]["status"] == "skipped-time-limit"
    assert not result["metrics"]["all_sources_fully_checked"]


def test_validation_of_profile_and_limits():
    for kwargs in ({"source_seconds": 0}, {"source_seconds": 121}, {"total_time_limit_seconds": float("nan")},
                   {"manuscript_scope": "unknown"}, {"config": {"max_gap": 6}}):
        with pytest.raises(ValueError):
            report("a b c", "a b c", **kwargs)


def test_original_pdf_highlights_actual_equal_words_not_gap_bounds(tmp_path):
    import hashlib
    import pymupdf
    from buna.pdf_reports import generate_pdf

    text = "red blue green black x pink gold silver y tan gray white"
    original = tmp_path / "original.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((60, 90), text, fontsize=10)
        pdf.save(original)
    with pymupdf.open(original) as pdf:
        target = document(pdf[0].get_text())
        original_words = pdf[0].get_text("words")
    result = improved_report(target, [{"id": "s", "document": document("red blue green black q pink gold silver r tan gray white")}])
    result["papers"] = [{"id": "s", "source_number": 1, "title": "Original synthetic source", "status": "compared"}]
    output = tmp_path / "report.pdf"
    mapping = generate_pdf({"original": str(original), "report": result, "job": {
        "filename": "Original synthetic.pdf", "document": target,
        "manuscript_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
    }}, output)
    assert mapping["unmapped_regions"] == 0
    with pymupdf.open(output) as pdf:
        page = pdf[mapping["summary_pages"]]
        quads = []
        for annot in page.annots() or []:
            if annot.type[1] == "Highlight":
                points = annot.vertices
                quads.extend(pymupdf.Quad(points[i:i + 4]).rect for i in range(0, len(points), 4))
        assert quads
        for x0, y0, x1, y1, word, *_ in original_words:
            marked = any(quad.intersects(pymupdf.Rect(x0, y0, x1, y1)) for quad in quads)
            assert marked is (word not in {"x", "y"}), word
