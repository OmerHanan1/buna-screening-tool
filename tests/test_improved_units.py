"""Synthetic staged operator/citation/edge fixtures; no private-paper text."""
import pytest

from buna.classified import tokens as old_tokens
from buna.documents import _structure
from buna.improved_eng import _content, improved_report
from buna.improved_tokens import tokens


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
