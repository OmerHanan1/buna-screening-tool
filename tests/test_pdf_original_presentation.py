import hashlib
import json
from pathlib import Path

import pymupdf
import pytest

from buna.pdf_reports import generate_pdf

TEXT = "Amber birds gather beside quiet rivers during winter mornings."


def original_pdf(path: Path):
    with pymupdf.open() as document:
        for index in range(2):
            page = document.new_page(width=640, height=820)
            page.insert_text((60, 35), f"Original running header {index + 1}", fontname="tiro", fontsize=11)
            page.insert_text((60, 95), TEXT, fontname="tiro", fontsize=12)
            page.insert_text((340, 145), "Original right-hand column", fontname="cour", fontsize=10)
            page.draw_rect(pymupdf.Rect(65, 180, 275, 280), color=(0, 0, 0), fill=(.85, .85, .85))
            for y in (180, 213, 246):
                page.insert_text((75, y + 20), f"Table row {y}", fontsize=9)
            image = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 30, 20))
            image.clear_with(180)
            page.insert_image(pymupdf.Rect(360, 200, 480, 280), pixmap=image)
            page.insert_text((60, 790), f"Original footer {index + 1}", fontsize=9)
        document[1].set_rotation(90)
        document.save(path)


def saved_payload(original, model):
    with pymupdf.open(original) as document:
        pages = [{"number": i + 1, "text": page.get_text()} for i, page in enumerate(document)]
    start = pages[0]["text"].index(TEXT)
    report = {
        "comparison_model": model,
        "algorithm_version": "improvedEng-v3.1-precision" if model == "improvedEng" else model,
        "metrics": {"eligible_words": 100, "score_denominator_words": 100, "overlapping_words": 9,
                    "overlap_percent": 9.0, "sources_compared": 1},
        "papers": [{"id": "1", "title": "Original synthetic source", "status": "compared"}],
        "source_coverage": [{"source_id": "1", "status": "compared", "overlap_percent": 9.0}],
        "matches": [{"source_id": "1", "match_kind": "exact", "kind": "exact",
                     "manuscript": {"start": start, "text": TEXT, "highlights": [[0, len(TEXT)]],
                                    "equal_spans": [[start, start + len(TEXT)]]},
                     "source": {"text": TEXT, "page": 1, "section": "Body"}}],
    }
    if model != "validated-lexical":
        report["classification"] = {"metrics": {"exact_words": 9, "similar_only_words": 0,
                                                   "combined_words": 9, "all_sources_fully_checked": True}}
    return {"report": report, "job": {"filename": "Original synthetic manuscript.pdf", "document": {"pages": pages},
            "manuscript_sha256": hashlib.sha256(original.read_bytes()).hexdigest()}, "original": str(original)}


@pytest.mark.parametrize("model", ["validated-lexical", "classified-v1.1", "improvedEng"])
def test_original_geometry_fonts_figures_and_tables_remain_the_manuscript(model, tmp_path):
    source = tmp_path / "original.pdf"
    original_pdf(source)
    payload = saved_payload(source, model)
    before = json.dumps(payload["report"], sort_keys=True).encode()
    output = tmp_path / f"{model}.pdf"
    mapping = generate_pdf(payload, output)
    assert mapping["render_mode"] == "original-pdf"
    assert mapping["original_manuscript_pages"] == 2 and mapping["unmapped_regions"] == 0
    assert json.dumps(payload["report"], sort_keys=True).encode() == before
    with pymupdf.open(source) as original, pymupdf.open(output) as result:
        assert len(result) == mapping["summary_pages"] + 2
        assert result.xref_get_key(result.pdf_catalog(), "OpenAction")[0] == "null"
        assert result.get_toc()[0][2] == 1
        for index, page in enumerate(original):
            copied = result[index + mapping["summary_pages"]]
            assert copied.rect == page.rect and copied.rotation == page.rotation
            assert copied.mediabox == page.mediabox and copied.cropbox == page.cropbox
            assert len(copied.get_images()) == len(page.get_images()) == 1
            assert {font[3] for font in page.get_fonts()} <= {font[3] for font in copied.get_fonts()}
            clip = None if model == "improvedEng" else pymupdf.Rect(40, 0, page.rect.width, page.rect.height)
            expected = page.get_pixmap(annots=False, clip=clip)
            actual = copied.get_pixmap(annots=False, clip=clip)
            assert (expected.width, expected.height) == (actual.width, actual.height)
            differences = [abs(a - b) for a, b in zip(expected.samples, actual.samples)]
            # PDF compositing can round antialiased text channels by two levels.
            assert max(differences) <= 2 and sum(differences) / len(differences) < .01
            if model == "improvedEng":
                assert [result.xref_stream(x) for x in copied.get_contents()] == [original.xref_stream(x) for x in page.get_contents()]
        first_page = result[mapping["summary_pages"]]
        annotations = list(first_page.annots())
        assert any(a.type[1] == "Highlight" for a in annotations)
        assert any(a.type[1] == "FreeText" for a in annotations) is (model == "improvedEng")
        for annotation in annotations:
            if annotation.type[1] == "Highlight":
                assert "Source: #1" in annotation.info["content"]
                assert "Overlapped text:" in annotation.info["content"]
        if model == "improvedEng":
            for index, page in enumerate(original):
                copied = result[index + mapping["summary_pages"]]
                for annotation in list(copied.annots() or []):
                    copied.delete_annot(annotation)
                assert copied.get_text() == page.get_text()


def test_original_unavailable_remains_an_explicit_fallback(tmp_path):
    path = tmp_path / "original.pdf"
    original_pdf(path)
    payload = saved_payload(path, "improvedEng")
    payload["original"] = str(tmp_path / "missing.pdf")
    result = generate_pdf(payload, tmp_path / "fallback.pdf")
    assert result["render_mode"] == "saved-text"
    assert "Original PDF is unavailable" in result["warning"]
    with pymupdf.open(tmp_path / "fallback.pdf") as document:
        assert "Original PDF is unavailable" in "".join(p.get_text() for p in document)


def test_improved_rotated_page_uses_original_unrotated_glyph_coordinates(tmp_path):
    source = tmp_path / "rotated.pdf"
    original_pdf(source)
    payload = saved_payload(source, "improvedEng")
    pages = payload["job"]["document"]["pages"]
    start = len(pages[0]["text"]) + 2 + pages[1]["text"].index(TEXT)
    match = payload["report"]["matches"][0]
    match["manuscript"].update(start=start, equal_spans=[[start, start + len(TEXT)]])
    output = tmp_path / "rotated-report.pdf"
    mapping = generate_pdf(payload, output)
    assert mapping["unmapped_regions"] == 0
    with pymupdf.open(output) as document:
        page = document[mapping["summary_pages"] + 1]
        assert page.rotation == 90
        annotations = list(page.annots())
        assert any(annotation.type[1] == "Highlight" for annotation in annotations)
        assert any(annotation.type[1] == "FreeText" for annotation in annotations)
