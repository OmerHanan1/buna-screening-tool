"""Synthetic long-source-key regression for the deployed Linux PDF renderer."""
import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile

import pymupdf

from buna.pdf_reports import generate_pdf


def verify():
    results = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        original = root / "original.pdf"
        text = "Original synthetic manuscript sentence for precise annotation verification."
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((60, 90), text)
            document.save(original)
        for count in (57, 94, 144):
            papers = [{"id": str(i), "source_number": i, "status": "compared",
                       "title": f"Unique synthetic source {i:03} " + "Scientific wording and emotional regulation " * (i % 4 + 1)}
                      for i in range(1, count + 1)]
            papers[35]["title"] += "Large multi-line source title " * 90
            coverage = [{"source_id": p["id"], "status": "compared", "overlap_percent": 0} for p in papers]
            coverage[-2].update(status="compared-with-limits", overlap_percent=1.25)
            coverage[-1].update(status="unavailable", reason="Synthetic source unavailable.")
            report = {"papers": papers, "source_coverage": coverage, "warnings": [], "algorithm_version": "2.5.3",
                      "metrics": {"overlap_percent": 5.48, "eligible_words": 1000, "sources_compared": count - 2},
                      "matches": [{"source_id": "1", "manuscript": {"text": text, "start": 0, "highlights": [[0, len(text)]]}}]}
            unchanged = copy.deepcopy(report)
            path = root / f"{count}.pdf"
            mapping = generate_pdf({"original": str(original), "report": report, "job": {
                "filename": "Original synthetic manuscript.pdf", "manuscript_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
                "document": {"pages": [{"number": 1, "text": text}]}}}, path)
            assert report == unchanged
            with pymupdf.open(path) as document:
                summary = "\n".join(document[i].get_text() for i in range(mapping["summary_pages"]))
                identifiers = re.findall(r"(?m)^#(\d+)\s*$", summary)
                assert identifiers == [str(i) for i in range(1, count + 1)], {
                    "pymupdf": pymupdf.VersionBind, "expected": count, "rendered_ids": identifiers}
                assert "5.48%" in summary and "0.00%" in summary and "1.25%" in summary
                assert "Syntheticsourceunavailable." in re.sub(r"\s+", "", summary)
                manuscript = document[mapping["summary_pages"]]
                assert text in manuscript.get_text() and list(manuscript.annots() or [])
            results.append({"sources": count, "all_rows_rendered_once": True, "summary_pages": mapping["summary_pages"],
                            "saved_scores_unchanged": True, "manuscript_annotations_preserved": True})
    return {"pymupdf": pymupdf.VersionBind, "results": results}


if __name__ == "__main__":
    print(json.dumps(verify()))
