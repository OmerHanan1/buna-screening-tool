import copy
import re

import pymupdf
import pytest

from buna.pdf_reports import generate_pdf, _typeset, _summary_table, _verify_summary_sources, PdfReportError


def fixture(count):
    papers = [{"id": str(i), "source_number": i, "title": f"Unique source {i:03} " + "Original scientific wording and emotional regulation " * (i % 4 + 1),
               "status": "compared", "overlap_percent": 0} for i in range(1, count + 1)]
    coverage = [{"source_id": p["id"], "status": "compared", "overlap_percent": 0} for p in papers]
    coverage[-2].update(status="compared-with-limits", reason="Source time limit.")
    coverage[-1].update(status="unavailable", reason="Synthetic PDF could not be extracted.")
    report = {"papers": papers, "source_coverage": coverage, "matches": [], "warnings": [],
              "algorithm_version": "2.5.3", "metrics": {"eligible_words": 1000, "score_denominator_words": 1000,
                  "overlap_percent": 5.48, "overlapping_words": 54, "sources_compared": count - 2}}
    return report


@pytest.mark.parametrize("count", [57, 94, 144])
def test_every_long_table_source_is_rendered_once(count, tmp_path):
    report = fixture(count)
    original_report = copy.deepcopy(report)
    job = {"filename": "Original synthetic manuscript.pdf", "document": {"pages": [{"number": 1, "text": "Original synthetic manuscript content."}]}}
    output = tmp_path / "report.pdf"
    result = generate_pdf({"job": job, "report": report}, output)
    with pymupdf.open(output) as document:
        text = "\n".join(document[i].get_text() for i in range(result["summary_pages"]))
        identifiers = re.findall(r"(?m)^#(\d+)\s*$", text)
        assert identifiers == [str(i) for i in range(1, count + 1)]
        assert "5.48%" in text
        assert "0.00%" in text
        assert "SyntheticPDFcouldnotbeextracted." in re.sub(r"\s+", "", text)
        assert "Notchecked" in re.sub(r"\s+", "", text)
        assert text.count("Paper\nOverlap\nStatus") > 1
        assert "Original synthetic manuscript content." in document[result["summary_pages"]].get_text()
        assert len(document) == result["summary_pages"] + 1
    assert report == original_report


def test_extremely_long_title_and_status_do_not_drop_source_rows():
    report = fixture(57)
    report["papers"][35]["title"] = "Extreme synthetic title " + "Detailed scientific terminology " * 110
    report["source_coverage"][-1]["reason"] = "Bounded extraction failed. " * 80
    numbers = {p["id"]: p["source_number"] for p in report["papers"]}
    names = {p["id"]: p["title"] for p in report["papers"]}
    with _typeset(_summary_table(report, numbers, names)) as summary:
        _verify_summary_sources(summary, numbers)
        text = "\n".join(page.get_text() for page in summary)
        assert "#57" in text and "Uniquesource057" in re.sub(r"\s+", "", text)


def test_missing_or_duplicated_source_rows_fail_instead_of_publishing():
    with _typeset("<p>#1</p><p>#2</p>") as missing:
        with pytest.raises(PdfReportError, match="incomplete or duplicated"):
            _verify_summary_sources(missing, {"a": 1, "b": 2, "c": 3})
    with _typeset("<p>#1</p><p>#1</p>") as duplicate:
        with pytest.raises(PdfReportError, match="incomplete or duplicated"):
            _verify_summary_sources(duplicate, {"a": 1})


def test_title_containing_source_number_cannot_fool_structured_row_invariant():
    report = fixture(57)
    report["papers"][0]["title"] = "#2"
    report["papers"][1]["title"] = "source-row-57 #57"
    numbers = {p["id"]: p["source_number"] for p in report["papers"]}
    with _typeset(_summary_table(report, numbers, {p["id"]: p["title"] for p in report["papers"]})) as summary:
        _verify_summary_sources(summary, numbers)
