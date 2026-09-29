"""Fully labelled synthetic suite for the isolated classified-v1 candidate."""
import copy
import itertools
import unicodedata

import pytest

from buna.classified import DEFAULT_CONFIG, classify_documents, tokens
from buna.comparison import ComparisonCancelled
from buna.documents import _structure

NINE = "amber birds gather beside quiet rivers during winter mornings"
LONG = ("researchers carefully measured individual learning outcomes across all participating schools "
        "using calibrated instruments and standardized procedures")
FILLER = "Unrelated opening words appear here first."


def doc(*pages):
    return _structure([{"number": i + 1, "text": text} for i, text in enumerate(pages)], "Synthetic", [])


def run(target_text, *source_texts, pages=None, **kwargs):
    target = doc(*(pages or [target_text]))
    sources = [{"id": f"s{i}", "document": doc(text)} for i, text in enumerate(source_texts)]
    kwargs.setdefault("manuscript_scope", "whole-document")
    return classify_documents(target, sources, **kwargs)


def kinds(result):
    return sorted((m["match_kind"], m["matched_words"]) for m in result["matches"] if not m["excluded_from_score"])


# EXACT path ---------------------------------------------------------------

def test_exact_nine_counts_and_eight_does_not():
    assert kinds(run(FILLER + " " + NINE + ".", "Source says " + NINE + " today.")) == [("exact", 9)]
    eight = " ".join(NINE.split()[:8])
    result = run(FILLER + " " + eight + ".", "Source says " + eight + " today.")
    assert result["matches"] == [] and result["metrics"]["exact_words"] == 0


def test_exact_is_maximal_across_long_runs_and_pages_without_duplicates():
    words = ["term" + str(i) for i in range(400)]
    target = doc(" ".join(words[:150]), " ".join(words[150:]) + " end.")
    result = classify_documents(target, [{"id": "s", "document": doc("Lead " + " ".join(words) + " tail.")}],
                                manuscript_scope="whole-document")
    assert [m["matched_words"] for m in result["matches"]] == [400]
    m = result["matches"][0]["manuscript"]
    assert m["page"] == 1 and m["text"] == target["text"][m["start"]:m["end"]]
    assert result["metrics"]["exact_words"] == 400


def test_exact_run_split_by_one_changed_word_is_two_runs_and_similar_does_not_relabel_them():
    changed = LONG.replace("instruments", "devices")
    result = run(FILLER + " " + LONG + ".", "Source: " + changed + ".")
    exact = [m for m in result["matches"] if m["match_kind"] == "exact"]
    # 12 equal words precede the substitution and only 3 follow it (< 9, not exact).
    assert [m["matched_words"] for m in exact] == [12]
    assert result["metrics"]["exact_words"] == 12


def test_changed_numbers_are_not_exact():
    left = "The mean score was 4.72 with standard deviation 2.42 across all participants in the sample."
    right = "The mean score was 4.16 with standard deviation 2.40 across all participants in the sample."
    result = run(left, right, config={"similar": {"enabled": False}})
    for m in result["matches"]:
        spans = [result_text[a:b] for result_text in [doc(left)["text"]] for a, b in m["manuscript"]["equal_spans"]]
        assert not any("72" in s or "42" in s for s in spans)


def test_unicode_combining_and_line_break_hyphen_are_exact():
    composed = "caf\u00e9 visitors " + NINE
    decomposed = "cafe\u0301 visitors " + NINE
    assert kinds(run(FILLER + " " + composed + ".", "Source " + decomposed + ".")) == [("exact", 11)]
    assert tokens("cafe\u0301")[0][0] == "caf\u00e9"
    wrapped = "amber birds gather beside quiet riv-\ners during winter mornings"
    assert kinds(run(FILLER + " " + wrapped + ".", "Source " + NINE + " end.")) == [("exact", 9)]


def test_hash_collisions_are_verified(monkeypatch):
    import buna.classified as c
    monkeypatch.setattr(c, "hash", lambda value: 0, raising=False)
    result = run(FILLER + " " + NINE + ".", "Different words entirely across the source text here now.", NINE + ".")
    assert [m["source_id"] for m in result["matches"]] == ["s1"]


# SIMILAR path -------------------------------------------------------------

SENT = "Participants reported their age gender and political identity at the end of the online survey session"


@pytest.mark.parametrize("variant, expected", [
    (SENT.replace("political", "religious"), True),                          # one substitution
    (SENT.replace("online survey", "long survey"), True),
    (SENT.replace("their age", "their exact age"), True),                    # one insertion
    (SENT.replace("political identity at the end", "political identity near the end"), True),
    ("At the end of the online survey session participants reported their age gender and political identity", True),  # block reorder
])
def test_similar_positives(variant, expected):
    result = run(FILLER + " " + SENT + ".", "Source intro. " + variant + ". Other text.")
    similar = [m for m in result["matches"] if m["match_kind"] == "similar"]
    assert bool(similar) is expected
    for m in similar:
        assert m["matched_words"] >= 9 and m["edits"]["counted"] <= m["edits"]["allowed"] <= 4


@pytest.mark.parametrize("variant", [
    # topic-only: shared subject words, different wording
    "Survey respondents disclosed demographic details including political leanings and age brackets online",
    # arbitrary bag of the same words
    "session survey online the of end the at identity political and gender age their reported participants",
    # too many changes for the hard ceiling
    "Participants later disclosed only their exact age and also gender plus the political orientation near the very end of an online survey",
])
def test_similar_negatives(variant):
    result = run(FILLER + " " + SENT + ".", "Source intro. " + variant + ". Other text.")
    assert [m for m in result["matches"] if m["match_kind"] == "similar"] == []


def test_opposite_assertion_is_still_only_wording_similarity():
    positive = "The results clearly showed that abstract processing reduced the intensity of moral disgust in adults"
    negative = "The results clearly showed that abstract processing increased the intensity of moral disgust in adults"
    result = run(FILLER + " " + positive + ".", "Source " + negative + ".")
    assert {m["match_kind"] for m in result["matches"]} <= {"exact", "similar"}
    assert "not a paraphrase" in result["labels"]["similar"]


def test_similar_reports_only_equal_words_as_matched_spans():
    variant = SENT.replace("political", "religious")
    result = run(FILLER + " " + SENT + ".", "Source intro. " + variant + ".")
    target = doc(FILLER + " " + SENT + ".")["text"]
    m = next(m for m in result["matches"] if m["match_kind"] == "similar")
    marked = " ".join(target[a:b] for a, b in m["manuscript"]["equal_spans"])
    assert "political" not in marked and "Participants" in marked


def test_synthetic_workshop_example_default_covers_local_part_only():
    left = "The workshop accepted only local artists who selected cedar as their first material and had a completion rate above 90%."
    right = "We limited the workshop to regional artists with cedar as their first material, and with a past completion rate of evening studio classes above 90%."
    similar = [m for m in run(left, right)["matches"] if m["match_kind"] == "similar"]
    # Default: "…first material and [had→with] a [+past] completion rate" = 9 equal words, 2 edits.
    assert [(m["matched_words"], m["edits"]["counted"]) for m in similar] == [(9, 2)]
    marked = [left[a:b] for a, b in similar[0]["manuscript"]["equal_spans"]]
    assert marked == ["cedar as their first material and", "a completion rate"]
    # Covering "above 90%" too needs 9 edits: only a much looser configuration does that.
    loose = run(left, right, config={"similar": {"max_edit_fraction": 0.5, "max_word_edits_ceiling": 10}})
    assert max(m["matched_words"] for m in loose["matches"] if m["match_kind"] == "similar") == 14


def test_exact_union_is_invariant_to_similar_settings():
    text = FILLER + " " + NINE + ". " + SENT + ". " + LONG + "."
    source = "Intro " + NINE + " x. " + SENT.replace("political", "religious") + ". " + LONG + " y."
    base = run(text, source)
    for params in [{"enabled": False}, {"max_word_edits": 0, "max_edit_fraction": 0}, {"max_edit_fraction": 0.5, "max_word_edits_ceiling": 20}]:
        other = run(text, source, config={"similar": params})
        assert other["metrics"]["exact_words"] == base["metrics"]["exact_words"]
        assert [m for m in other["matches"] if m["match_kind"] == "exact"] == [m for m in base["matches"] if m["match_kind"] == "exact"]


# Sources, states, coverage ------------------------------------------------

def test_multi_source_and_repeated_source_locations_are_kept_and_order_is_deterministic():
    target = FILLER + " " + NINE + "."
    sources = ["A " + NINE + " then " + NINE + " again.", "B " + NINE + "."]
    results = []
    for order in itertools.permutations(range(2)):
        docs = [{"id": f"s{i}", "document": doc(sources[i])} for i in order]
        results.append(classify_documents(doc(target), docs, manuscript_scope="whole-document"))
    assert results[0]["matches"] == results[1]["matches"]
    assert sorted(m["source_id"] for m in results[0]["matches"]) == ["s0", "s0", "s1"]
    assert results[0]["metrics"]["exact_words"] == 9
    exact_interval = next(i for i in results[0]["coverage_intervals"] if i["state"] == "exact")
    assert exact_interval["sources"] == ["s0", "s1"]


def test_other_source_is_searched_for_similarity_even_if_one_source_is_exact():
    target = FILLER + " " + SENT + "."
    result = run(target, "Intro " + SENT + " end.", "Intro " + SENT.replace("political", "religious") + " end.")
    assert {(m["source_id"], m["match_kind"]) for m in result["matches"]} >= {("s0", "exact"), ("s1", "similar")}
    assert result["metrics"]["similar_only_words"] == 0  # exact precedence in the word-state view


def test_states_exclusions_unmatched_and_front_matter():
    pages = ["Title page. Department of Psychology, Example University. " + NINE + ".",
             "Abstract\nOriginal abstract words are written here for the study today.\n\n"
             "“" + LONG + "”\n\nReferences\n" + SENT]
    target = doc(*pages)
    result = classify_documents(target, [{"id": "s", "document": doc(NINE + ". " + LONG + ". " + SENT + ".")}])
    states = {i["state"] for i in result["coverage_intervals"]}
    assert {"excluded-front-matter", "excluded-quotation", "excluded-bibliography", "unmatched"} <= states
    assert result["metrics"]["exact_words"] == 0 and result["metrics"]["combined_words"] == 0
    assert result["manuscript_scope"]["applied"] == "abstract-onward"
    assert all(m["excluded_from_score"] for m in result["matches"])


def test_unavailable_or_partial_source_makes_unmatched_text_not_fully_checked():
    target = doc(FILLER + " " + NINE + ". More text here.")
    result = classify_documents(target, [{"id": "a", "document": doc(NINE + ".")}, {"id": "b"}],
                                manuscript_scope="whole-document")
    states = {i["state"] for i in result["coverage_intervals"]}
    assert "unmatched" not in states and "not-fully-checked" in states
    assert not result["metrics"]["all_sources_fully_checked"]
    partial = classify_documents(target, [{"id": "a", "document": doc(NINE + ".")}], source_seconds=0,
                                 manuscript_scope="whole-document")
    assert partial["source_coverage"][0]["status"] == "compared-with-limits"
    assert partial["metrics"]["unmatched_words"] == 0


def test_no_heading_fallback_and_intervals_are_compact():
    result = run(FILLER + " " + NINE + ".", NINE + ".", manuscript_scope="abstract-onward")
    assert result["manuscript_scope"]["applied"] == "whole-document"
    assert len(result["coverage_intervals"]) <= 3


def test_identical_manuscript_copy_is_excluded():
    text = FILLER + " " + NINE + "."
    result = run(text, text)
    assert result["source_coverage"][0]["status"] == "excluded-identical" and not result["matches"]


def test_inputs_not_mutated_and_cancellation():
    target = doc(FILLER + " " + NINE + ".")
    sources = [{"id": "s", "document": doc(NINE + ".")}]
    before = copy.deepcopy((target, sources))
    classify_documents(target, sources)
    assert (target, sources) == before
    with pytest.raises(ComparisonCancelled):
        classify_documents(target, sources, cancelled=lambda: True)


def test_default_config_is_declared():
    assert DEFAULT_CONFIG["min_exact_words"] == 9
    assert DEFAULT_CONFIG["similar"]["max_word_edits_ceiling"] == 4


def test_repetitive_text_is_bounded_and_disclosed(monkeypatch):
    import buna.classified as c
    monkeypatch.setattr(c, "MAX_EVIDENCE_BYTES", 200_000)
    phrase = NINE + " "
    result = run(FILLER + " " + phrase * 60, "Lead " + phrase * 300)
    row = result["source_coverage"][0]
    assert row["status"] == "compared-with-limits" and "evidence-memory" in row["reason"]
    assert result["metrics"]["unmatched_words"] == 0 and not result["metrics"]["all_sources_fully_checked"]


# Resource contract, attribution and adapter ----------------------------------

def test_total_time_limit_validation_and_skipped_sources(monkeypatch):
    import buna.classified as c
    with pytest.raises(ValueError):
        run(NINE, NINE, total_time_limit_seconds=0)
    with pytest.raises(ValueError):
        run(NINE, NINE, total_time_limit_seconds=601)
    clock = iter([0.0] + [1000.0] * 1000)
    monkeypatch.setattr(c.time, "monotonic", lambda: next(clock))
    result = run(FILLER + " " + NINE + ".", NINE + ".", NINE + ".", total_time_limit_seconds=5)
    assert [r["status"] for r in result["source_coverage"]] == ["skipped-time-limit"] * 2
    assert result["source_coverage"][0]["limits_reached"] == ["total-time-limit"]
    assert result["metrics"]["unmatched_words"] == 0 and result["metrics"]["not_fully_checked_words"] > 0


@pytest.mark.parametrize("stage", ["index", "seed", "candidates", "align"])
def test_deadline_is_checked_inside_single_source_loops(monkeypatch, stage):
    import buna.classified as c
    calls = {"n": 0}
    original = {"index": c._exact_runs, "seed": c._exact_runs, "candidates": c._similar, "align": c._align}[stage]
    name = {"index": "_exact_runs", "seed": "_exact_runs", "candidates": "_similar", "align": "_align"}[stage]

    def wrapped(*args):
        *rest, check = args
        def counting():
            calls["n"] += 1
            if calls["n"] > 2:
                raise c._Limit("total-time-limit", "The total comparison time budget ended during this source; retained matches are partial.")
            check()
        return original(*rest, counting)
    monkeypatch.setattr(c, name, wrapped)
    words = " ".join(f"tok{i}" for i in range(12000))
    target = FILLER + " " + (words if stage in ("index", "seed") else (SENT + ". ") * 6)
    source = words + " " + (NINE if stage in ("index", "seed") else SENT.replace("political", "religious") + ".")
    result = run(target, source)
    row = result["source_coverage"][0]
    assert row["status"] == "compared-with-limits" and row["limits_reached"] == ["total-time-limit"]
    assert calls["n"] > 2


def test_source_bibliography_is_not_matched():
    source = "Body text of the source.\n\nReferences\n" + NINE + " reference entry."
    assert run(FILLER + " " + NINE + ".", source)["matches"] == []


def test_attribution_axis_is_attached_and_independent_of_match_kind():
    cited = run(FILLER + " " + NINE + " (Smith, 2020).", "Source " + NINE + ".")
    m = cited["matches"][0]
    assert m["match_kind"] == "exact" and "citation-context" in m["flags"]
    assert m["classification"] == "Cited wording—quotation may be needed"
    quoted = run(FILLER + ' "' + NINE + '".', "Source " + NINE + ".")
    q = quoted["matches"][0]
    assert q["quotation"]["status"] == "recognized" and q["excluded_from_score"]
    assert q["classification"].startswith("Quotation")


def test_report_adapter_is_compatible_with_existing_consumers():
    from buna.classified import classify_report
    from buna.presentation import evidence_presentation, manuscript_reader
    from buna.result_state import result_state
    target = doc("Title page " + FILLER, "Abstract\n" + FILLER + " " + NINE + ". " + SENT + ".")
    sources = [{"id": "a", "source_number": 1, "document": doc("Intro " + NINE + " x.")},
               {"id": "b", "source_number": 2, "document": doc("Intro " + SENT.replace("political", "religious") + ".")}]
    report = classify_report(target, sources)
    report.update(papers=[{"id": "a", "source_number": 1, "status": "compared"}, {"id": "b", "source_number": 2, "status": "compared"}],
                  coverage={"compared": 2})
    assert report["metrics"]["overlapping_words"] == report["metrics"]["exact_words"] + report["metrics"]["similar_only_words"]
    assert report["settings"]["score_basis"] == "abstract-onward-word-units"
    assert {m["kind"] for m in report["matches"]} == {"exact", "near-verbatim"}
    assert {m["match_kind"] for m in report["matches"]} == {"exact", "similar"}
    for m in report["matches"]:
        p = m["manuscript"]
        assert p["text"] == target["text"][p["start"]:p["end"]]
        assert all(p["text"][a:b].strip() for a, b in p["highlights"])
        s = m["source"]
        src = next(x for x in sources if x["id"] == m["source_id"])["document"]["text"]
        assert all(src[s["start"] + a:s["start"] + b].strip() for a, b in s["highlights"])
    assert result_state(report)["score_available"]
    assert evidence_presentation(report)["groups"]
    assert manuscript_reader(report)["pages"]
    assert {i["state"] for i in report["classification"]["coverage_intervals"]} >= {"exact", "similar", "excluded-front-matter"}


def test_adapter_retains_extraction_and_no_abstract_warnings():
    from buna.classified import classify_report
    target = doc(FILLER + " " + NINE)
    target["warnings"].append("Synthetic extraction warning.")
    report = classify_report(target, [{"id": "s", "document": doc(NINE + ". Source.")}])
    assert "Synthetic extraction warning." in report["warnings"]
    assert any(w.startswith("Abstract heading not detected") for w in report["warnings"])
    assert report["algorithm_version"] == "classified-v1.1"


@pytest.mark.parametrize("sources", [[], [{"id": "excluded", "excluded": True}]])
def test_no_checked_sources_never_claim_unmatched(sources):
    result = classify_documents(doc(FILLER + " " + NINE), sources)
    assert result["metrics"]["unmatched_words"] == 0
    assert result["metrics"]["not_fully_checked_words"] > 0
    assert not result["metrics"]["all_sources_fully_checked"]


def test_size_guards_precede_matching():
    import buna.classified as candidate
    with pytest.raises(ValueError, match="Manuscript exceeds"):
        classify_documents({"text": "x" * 1_000_001}, [])
    result = classify_documents(doc(FILLER + NINE), [{"id": "large", "document": {"text": "x" * 2_000_001}}])
    assert result["source_coverage"][0]["status"] == "skipped-size-limit"
    assert result["metrics"]["unmatched_words"] == 0


def test_lazy_loader_does_not_retain_all_source_documents():
    import gc
    import weakref
    class Document(dict):
        pass
    references, live_counts = [], []
    def load(source):
        gc.collect()
        live_counts.append(sum(ref() is not None for ref in references))
        document = Document(doc("Source " + str(source["id"]) + " unrelated experimental observations."))
        references.append(weakref.ref(document))
        return document
    classify_documents(doc(FILLER + " " + NINE), [{"id": str(i)} for i in range(44)], load_document=load)
    assert max(live_counts) <= 1
    assert len(references) == 88


def test_similar_cannot_bridge_source_bibliography():
    target = FILLER + " " + NINE + "."
    source = "amber birds gather beside quiet\nReferences\nExcluded reference words\nAppendix\nrivers during winter mornings."
    result = run(target, source)
    assert not result["matches"]


def test_decimal_is_a_whole_unit_not_partially_exact():
    left = "We measured 4.72 amber birds gather beside quiet rivers during winter mornings."
    right = "We measured 4.16 amber birds gather beside quiet rivers during winter mornings."
    assert [word for word, _, _ in tokens("4.72 and 4.16")] == ["4.72", "and", "4.16"]
    for match in run(left, right)["matches"]:
        marked = " ".join(left[a:b] for a, b in match["manuscript"]["equal_spans"])
        assert "4" not in marked


def test_intervals_keep_similar_source_alternatives_under_exact_precedence():
    result = run(FILLER + " " + SENT + ".", "Intro " + SENT + ".", "Intro " + SENT.replace("political", "religious") + ".")
    assert any(interval["state"] == "exact" and set(interval["sources"]) == {"s0", "s1"}
               for interval in result["coverage_intervals"])


def test_exact_matches_stream_before_limit_and_include_pair_memory(monkeypatch):
    import buna.classified as candidate
    monkeypatch.setattr(candidate, "MAX_EVIDENCE_BYTES", 6000)
    result = run(FILLER + NINE, "First " + NINE + ". Middle. " + NINE + ".")
    assert result["matches"]
    assert result["source_coverage"][0]["status"] == "compared-with-limits"
    assert result["metrics"]["unmatched_words"] == 0


def test_working_index_budget_stops_before_growth(monkeypatch):
    import buna.classified as candidate
    monkeypatch.setattr(candidate, "WORKING_INDEX_BYTES", 100)
    result = run(FILLER + NINE, "Source " + NINE)
    assert "index-memory-limit" in result["source_coverage"][0]["limits_reached"]
    assert result["metrics"]["unmatched_words"] == 0


def test_deadline_during_lazy_prepass_marks_all_unknown(monkeypatch):
    import buna.classified as candidate
    clock = [0.]
    monkeypatch.setattr(candidate.time, "monotonic", lambda: clock[0])
    calls = []
    def load(source):
        calls.append(source["id"])
        clock[0] = 10
        return doc(NINE)
    result = classify_documents(doc(FILLER + NINE), [{"id": "a"}, {"id": "b"}],
                                load_document=load, total_time_limit_seconds=1)
    assert calls == ["a"]
    assert all(row["status"] == "skipped-time-limit" for row in result["source_coverage"])
    assert result["metrics"]["unmatched_words"] == 0


def test_classified_pdf_types_and_original_pages(tmp_path):
    import hashlib
    import pymupdf
    from buna.classified import classify_report
    from buna.pdf_reports import generate_pdf
    text = "Abstract\n" + NINE + ".\n" + SENT + "."
    target = doc(text)
    sources = [{"id": "a", "document": doc("Intro " + NINE + ".")},
               {"id": "b", "document": doc(SENT.replace("political", "religious") + ". Other.")}]
    report = classify_report(target, sources)
    report["papers"] = [{"id": "a", "title": "Synthetic exact source", "status": "compared"},
                        {"id": "b", "title": "Synthetic similar source", "status": "compared"}]
    original = tmp_path / "manuscript.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(45, 45, 550, 750), text, fontsize=10)
        document.save(original)
    before = hashlib.sha256(original.read_bytes()).hexdigest()
    destination = tmp_path / "report.pdf"
    mapping = generate_pdf({"job": {"document": target, "manuscript_sha256": before},
                            "report": report, "original": str(original)}, destination)
    assert mapping["unmapped_regions"] == 0
    assert hashlib.sha256(original.read_bytes()).hexdigest() == before
    with pymupdf.open(destination) as pdf:
        summary = unicodedata.normalize("NFKC", " ".join(pdf[0].get_text().split()))
        assert "Experimental exact + similar wording" in summary
        assert "not a finding of originality" in summary
        info = [annotation.info for page in pdf for annotation in page.annots() or []]
        assert any("Exact overlap" in item["title"] for item in info)
        assert any("Similar wording" in item["title"] for item in info)
        assert all(item["content"].startswith("Source: #") for item in info)


@pytest.mark.parametrize("rotation", [0, 90])
def test_pdf_exact_precedence_has_no_double_paint_and_keeps_source_alternatives(tmp_path, rotation):
    import pymupdf
    from buna.classified import classify_report
    from buna.pdf_reports import generate_pdf
    body = NINE + " extra observations"
    target = doc("Abstract\n" + body)
    report = classify_report(target, [
        {"id": "a", "document": doc("Source " + NINE + ".")},
        {"id": "b", "document": doc("Source " + body.replace("rivers", "streams") + ".")},
    ])
    assert report["metrics"]["exact_words"] == 9
    assert report["metrics"]["similar_only_words"] == 2
    assert report["metrics"]["overlapping_words"] == 11
    report["papers"] = [{"id": "a", "title": "Exact source", "status": "compared"},
                        {"id": "b", "title": "Edited source", "status": "compared"}]
    original = tmp_path / "original.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_textbox(pymupdf.Rect(45, 45, 550, 750), target["text"], fontsize=10)
        page.set_rotation(rotation)
        document.save(original)
    output = tmp_path / "classified.pdf"
    mapping = generate_pdf({"job": {"document": target}, "report": report, "original": str(original)}, output)
    with pymupdf.open(output) as pdf:
        assert "Exact overlap" in pdf[0].get_text() and "Similar wording" in pdf[0].get_text()
        page = pdf[mapping["summary_pages"]]
        annotations = list(page.annots())
        colors = {tuple(a.colors["stroke"]) for a in annotations}
        assert len(colors) == 2
        def covering(word):
            rect = page.search_for(word)[0]
            center = (rect.tl + rect.br) / 2
            return [a for a in annotations if any(pymupdf.Quad(a.vertices[i:i + 4]).rect.contains(center)
                                                  for i in range(0, len(a.vertices), 4))]
        for word in body.split():
            assert len(covering(word)) == 1, word
        assert "Exact overlap" in covering("amber")[0].info["title"]
        assert "Similar wording" in covering("extra")[0].info["title"]
        assert "Source: #1" in covering("amber")[0].info["content"]
        assert "Source: #2" in covering("amber")[0].info["content"]
        assert "streams" in covering("extra")[0].info["content"]
