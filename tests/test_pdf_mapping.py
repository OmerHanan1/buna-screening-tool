from buna.pdf_reports import PageMapper, map_region
from buna.pdf_reports import generate_pdf
import copy


def test_short_word_next_to_extractor_disagreement_uses_exact_block():
    saved = "LEFT FOOTNOTE A\nUnique cortical measurement data shows robust signals.\nSECOND COLUMN A"
    rendered = "DIFFERENT HEADER\nUnique cortical measurement data shows robust signals.\nSECOND COLUMN B"
    start = saved.index("Unique")
    indices, method = map_region(saved, rendered, start, start + 6)
    assert method == "unique-exact-block"
    assert "".join(rendered[i] for i in indices) == "Unique"


def test_ambiguous_identical_short_occurrences_are_not_guessed():
    saved = "unknown context signal end"
    rendered = "first context signal end unrelated second context signal end"
    assert map_region(saved, rendered, saved.index("signal"), saved.index("signal") + 6)[0] == []


def test_mapping_normalization_is_cached_per_page_and_keeps_hyphens():
    saved = "Unique inter-\nnational cortical signals provide stable measurements."
    glyphs = "Unique international cortical signals provide stable measurements."
    mapper = PageMapper(saved, glyphs)
    for word in ("cortical", "signals"):
        start = saved.index(word)
        indices, _ = mapper.map(start, start + len(word))
        assert "".join(glyphs[i] for i in indices) == word


def test_failed_mapping_never_changes_score_or_evidence(tmp_path):
    import hashlib
    import pymupdf
    original = tmp_path / "original.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((60, 90), "Unrelated original glyphs prevent reliable placement.")
        pdf.save(original)
    text = "Saved matched wording does not map to this original page."
    report = {
        "metrics": {"eligible_words": 10, "score_denominator_words": 10, "overlapping_words": 9, "overlap_percent": 90},
        "papers": [{"id": "1", "status": "compared", "title": "Synthetic source"}],
        "source_coverage": [{"source_id": "1", "status": "compared", "overlap_percent": 90}],
        "matches": [{"source_id": "1", "manuscript": {"start": 0, "text": text, "highlights": [[0, len(text)]]}}],
    }
    before = copy.deepcopy(report)
    mapping = generate_pdf({"report": report, "original": str(original), "job": {
        "filename": "Synthetic.pdf", "manuscript_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "document": {"pages": [{"number": 1, "text": text}]}}}, tmp_path / "report.pdf")
    assert mapping["unmapped_regions"] == 1
    assert mapping["failures"][0]["reason"]
    assert report == before
