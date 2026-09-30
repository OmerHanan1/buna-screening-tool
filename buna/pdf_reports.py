"""Local, cached PDF annotations using verified saved-text-to-glyph mappings."""
from __future__ import annotations

import bisect
import hashlib
import html
import json
import math
import os
import re
import subprocess
import sys
import threading
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from uuid import uuid4

import pymupdf as fitz

from buna.result_state import result_state
from buna.reports import _THEME

PDF_RENDERER_VERSION = "7"
_LOCK = threading.Lock()
def _theme(name: str) -> str:
    match = re.search(rf"--cp-{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", _THEME)
    if not match:
        raise PdfReportError(f"Missing PDF theme color: {name}")
    return match.group(1)


def _color(name: str) -> tuple[float, float, float]:
    value = _theme(name).lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


class PdfReportError(ValueError):
    pass


def _normalize(text: str) -> tuple[str, list[int]]:
    ignored_hyphens = {match.start() for match in re.finditer(r"(?<=[^\W\d_])-[ \t]*\r?\n[ \t]*(?=[a-z])", text)}
    normalized, indices = [], []
    punctuation = {"’": "'", "‘": "'", "“": '"', "”": '"', "\u2010": "-", "\u2011": "-"}
    for index, char in enumerate(text):
        if char.isspace() or char == "\u00ad" or index in ignored_hyphens:
            continue
        for unit in unicodedata.normalize("NFKD", punctuation.get(char, char)).casefold():
            normalized.append(unit)
            indices.append(index)
    return "".join(normalized), indices


def _glyphs(document: fitz.Document, page_indices: list[int]) -> tuple[str, list[dict | None]]:
    text, glyphs = [], []
    for page_index in page_indices:
        page = document[page_index]
        for block in page.get_text("rawdict", sort=False)["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    for char in span.get("chars", []):
                        value = char["c"]
                        try:
                            quad = fitz.recover_char_quad(line["dir"], span, char)
                        except (ValueError, RuntimeError):
                            quad = None
                        for unit in value:
                            text.append(unit)
                            glyphs.append({"page": page_index, "quad": quad,
                                           "line": (page_index, round(quad.rect.y0, 1), tuple(line["dir"]))} if quad else None)
                text.append("\n"); glyphs.append(None)
        text.append("\n"); glyphs.append(None)
    return "".join(text), glyphs


def map_region(saved: str, glyph_text: str, start: int, end: int) -> tuple[list[int], str]:
    """Accept exact normalized-page alignment or a unique exact local context.

    Never use fuzzy similarity to place an annotation. Ambiguity is a reported
    mapping failure, not permission to highlight a guessed occurrence.
    """
    if not 0 <= start < end <= len(saved):
        return [], "Saved range is outside the extracted page."
    source, positions = _normalize(saved)
    rendered, rendered_positions = _normalize(glyph_text)
    first, last = bisect.bisect_left(positions, start), bisect.bisect_left(positions, end)
    fragment = source[first:last]
    if not fragment:
        return [], "No visible characters in saved highlight."
    if source == rendered:
        return sorted(set(rendered_positions[first:last])), "exact-page"
    for padding in (32, 64, 128):
        left, right = max(0, first - padding), min(len(source), last + padding)
        context = source[left:right]
        at = rendered.find(context)
        if at >= 0 and rendered.find(context, at + 1) == -1:
            begin = at + first - left
            return sorted(set(rendered_positions[begin:begin + len(fragment)])), "unique-context"
    # A long, unique exact phrase is safe even when neighboring extraction order
    # differs. Short/common words require contextual verification above.
    at = rendered.find(fragment)
    if len(fragment) >= 20 and at >= 0 and rendered.find(fragment, at + 1) == -1:
        return sorted(set(rendered_positions[at:at + len(fragment)])), "unique-phrase"
    return [], "No unique exact glyph mapping; no highlight was placed."


def _regions(report: dict, text: str) -> tuple[list[dict], list[dict]]:
    events = defaultdict(Counter)
    failures = []
    for index, match in enumerate(report.get("matches", [])):
        if match.get("excluded_from_score"):
            continue
        passage = match.get("manuscript", {})
        for a, b in passage.get("scored_highlights", passage.get("highlights", [])):
            start, end = passage.get("start", 0) + a, passage.get("start", 0) + b
            if not (0 <= a < b <= len(passage.get("text", "")) and 0 <= start < end <= len(text)) \
                    or text[start:end] != passage["text"][a:b]:
                failures.append({"match_index": index, "source_id": str(match["source_id"]),
                                 "reason": "Saved text and offsets disagree; no guessed annotation."})
                continue
            events[start][str(match["source_id"])] += 1
            events[end][str(match["source_id"])] -= 1
    active, regions, previous = Counter(), [], None
    for point in sorted(events):
        if previous is not None and point > previous:
            sources = tuple(sorted(sid for sid, count in active.items() if count > 0))
            if sources:
                if regions and regions[-1]["sources"] == sources and text[regions[-1]["end"]:previous].isspace():
                    regions[-1]["end"] = point
                elif regions and regions[-1]["sources"] == sources and regions[-1]["end"] == previous:
                    regions[-1]["end"] = point
                else:
                    regions.append({"start": previous, "end": point, "sources": sources})
        active.update(events[point])
        previous = point
    return regions, failures


def _sequences(report: dict, text: str, *, link_overlaps: bool = True) -> tuple[list[dict], list[dict]]:
    groups = {}
    failures = []
    for index, match in enumerate(report.get("matches", [])):
        if match.get("excluded_from_score"):
            continue
        passage = match.get("manuscript", {})
        spans = []
        for a, b in passage.get("scored_highlights", passage.get("highlights", [])):
            start, end = passage.get("start", 0) + a, passage.get("start", 0) + b
            if not (0 <= a < b <= len(passage.get("text", "")) and 0 <= start < end <= len(text)) \
                    or text[start:end] != passage["text"][a:b]:
                failures.append({"match_index": index, "source_id": str(match["source_id"]),
                                 "reason": "Saved text and offsets disagree; no guessed annotation."})
                continue
            spans.append((start, end))
        if not spans:
            continue
        spans = sorted(set(spans))
        merged = []
        for start, end in spans:
            if merged and (start <= merged[-1][1] or text[merged[-1][1]:start].isspace()):
                merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
            else:
                merged.append((start, end))
        # Same saved target occurrence + same marked subspans share a comment;
        # distinct source occurrences are separate entries, not overwritten.
        key = tuple(merged)
        group = groups.setdefault(key, {"spans": merged, "matches": [], "sources": set()})
        group["matches"].append(match)
        group["sources"].add(str(match["source_id"]))
    sequences = sorted(groups.values(), key=lambda group: group["spans"])
    for i, sequence in enumerate(sequences if link_overlaps else []):
        for other in sequences[i + 1:]:
            if other["spans"][0][0] >= sequence["spans"][-1][1]:
                break
            if any(a < d and c < b for a, b in sequence["spans"] for c, d in other["spans"]):
                sequence.setdefault("overlapping_matches", []).extend(other["matches"])
                other.setdefault("overlapping_matches", []).extend(sequence["matches"])
    return sequences, failures


def _classified_sequences(report: dict, text: str) -> tuple[list[dict], list[dict]]:
    """One visual layer per original character span, with exact precedence."""
    original, failures = _sequences(report, text, link_overlaps=False)
    events = defaultdict(Counter)
    for index, sequence in enumerate(original):
        for start, end in sequence["spans"]:
            events[start][index] += 1
            events[end][index] -= 1
    active, previous, groups = Counter(), None, {}
    for point in sorted(events):
        indices = tuple(sorted(index for index, count in active.items() if count > 0))
        if indices and previous is not None and point > previous:
            matches = [match for index in indices for match in original[index]["matches"]]
            kind = "exact" if any(match.get("match_kind") == "exact" for match in matches) else "similar"
            key = kind, indices
            group = groups.setdefault(key, {"spans": [], "matches": matches,
                                            "sources": {str(match["source_id"]) for match in matches},
                                            "visual_kind": kind})
            if group["spans"] and group["spans"][-1][1] == previous:
                group["spans"][-1] = group["spans"][-1][0], point
            else:
                group["spans"].append((previous, point))
        active.update(events[point])
        previous = point
    return sorted(groups.values(), key=lambda group: group["spans"]), failures


def _sequence_comments(sequence: dict, numbers: dict[str, int]) -> list[str]:
    """Bound comments to original passages, preserving every alternative entry."""
    entries = []
    seen = set()
    for match in sorted(sequence["matches"] + sequence.get("overlapping_matches", []),
                        key=lambda m: (numbers.get(str(m["source_id"]), 0), m.get("source", {}).get("start", 0))):
        sid = str(match["source_id"])
        passage = match.get("source") or {}
        key = (sid, passage.get("start"), passage.get("end"), passage.get("text", ""), match.get("match_kind"))
        if key in seen:
            continue
        seen.add(key)
        context = str(passage.get("text", ""))
        relative_start = passage.get("match_start", passage.get("start", 0)) - passage.get("start", 0)
        relative_end = passage.get("match_end", passage.get("end", passage.get("start", 0) + len(context))) - passage.get("start", 0)
        if 0 <= relative_start < relative_end <= len(context):
            context = context[relative_start:relative_end]
        if not context:
            context = "[Original source excerpt was not saved; inspect the evidence JSON.]"
        heading = f"Source: #{numbers[sid]}\nOverlapped text: " if sid in numbers else "Source: Not recorded\nOverlapped text: "
        if len(context) <= 10000:
            entries.append(heading + context)
        else:
            pieces = [context[i:i + 10000] for i in range(0, len(context), 10000)]
            for piece in pieces:
                entries.append(heading + piece)
    comments, current = [], ""
    for entry in entries:
        if len(current) + len(entry) > 14000 and current.strip():
            comments.append(current)
            current = ""
        current += ("\n\n" if current else "") + entry
    if current.strip():
        comments.append(current)
    return comments or ["Source: Not recorded\nOverlapped text: Original source excerpt unavailable."]


def _set_comment_popup(page: fitz.Page, annotation: fitz.Annot) -> None:
    bounds = page.rect * page.derotation_matrix
    margin = min(12, bounds.width / 10, bounds.height / 10)
    width, height = min(420, bounds.width - 2 * margin), min(480, bounds.height - 2 * margin)
    x = bounds.x1 - margin - width
    y = max(bounds.y0 + margin, min(bounds.y1 - margin - height, annotation.rect.y0))
    annotation.set_popup(fitz.Rect(x, y, x + width, y + height))
    annotation.set_open(False)
    annotation.update()


def _reply_position(page: fitz.Page) -> fitz.Point | None:
    bounds = page.rect * page.derotation_matrix
    occupied = [fitz.Rect(block[:4]) for block in page.get_text("blocks")]
    occupied.extend(a.rect for a in page.annots() or [] if a.type[1] == "Text")
    for y in range(24, int(bounds.height) - 24, 24):
        for x in (bounds.width - 24, 4):
            rect = fitz.Rect(x, y, x + 20, y + 20)
            if bounds.contains(rect) and not any(rect.intersects(area) for area in occupied):
                return rect.tl
    return None


def _typeset(body: str) -> fitz.Document:
    # MuPDF's PDF typesetter consumes resolved colors, not CSS custom properties.
    css = """
    body { font-family: sans-serif; font-size: 10pt; color: TEXT; line-height: 1.4; }
    h1 { font-size: 20pt; } h2 { font-size: 12pt; margin-top: 16pt; }
    p { margin: 6pt 0; } table { width: 100%; border-collapse: collapse; }
    td, th { padding: 5pt; text-align: left; vertical-align: top; border-bottom: 0.5pt solid BORDER; }
    .muted { color: MUTED; } .warn { border-left: 2pt solid ACCENT; padding-left: 8pt; }
    """.replace("TEXT", _theme("text")).replace("BORDER", _theme("border")).replace("MUTED", _theme("text-muted")).replace("ACCENT", _theme("accent"))
    story = fitz.Story(html=body, user_css=css)
    row_positions = []
    def position(element):
        if element.id and element.id.startswith("source-row-"):
            row_positions.append((element.id.removeprefix("source-row-"), element.open_close))
    document = story.write_with_links(lambda rect_num, filled: (fitz.Rect(0, 0, 595, 842), fitz.Rect(36, 36, 559, 806), None),
                                      positionfn=position)
    document._source_row_positions = row_positions
    return document


def _percent(value: object) -> str:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "Not assessed"
    return f"{value:.2f}%"


def _summary_table(report: dict, numbers: dict[str, int], names: dict[str, str]) -> str:
    outcome = result_state(report)
    metrics = report.get("metrics", {})
    source_results = {str(row["source_id"]): row for row in report.get("source_coverage", [])}
    score = _percent(metrics.get("overlap_percent")) if outcome["score_available"] else "Not assessed"
    status = {
        "complete": "Complete", "partial": "Partial", "all_excluded": "All text excluded",
        "unscorable": "No scoring denominator", "not_compared": "No papers checked",
    }.get(outcome["state"], outcome["heading"])
    if outcome["score_kind"] == "lower_bound":
        status += " · lower bound"
    rows = [f"<tr><td>—</td><td><b>Overall overlap</b></td><td><b>{score}</b></td>"
            f"<td><b>{html.escape(status)}</b></td></tr>"]
    for paper in report.get("papers", []):
        sid = str(paper["id"])
        result = source_results.get(sid, {})
        source_status = result.get("status") or paper.get("comparison_status") or (
            "compared-with-limits" if paper.get("partial_comparison") else paper.get("status", "unknown"))
        checked = source_status in {"compared", "compared-with-limits"}
        label = "Checked" if source_status == "compared" else (
            "Partial · lower bound" if source_status == "compared-with-limits" else
            "Excluded" if str(source_status).startswith("excluded") or paper.get("excluded") else "Not checked")
        value = result.get("overlap_percent", paper.get("overlap_percent"))
        overlap = _percent(value) if checked and outcome["score_available"] else "Not assessed"
        if checked and outcome["score_available"] and value is None:
            overlap = "Not recorded"
        reason = result.get("reason")
        if not checked and isinstance(reason, str) and reason:
            label += " · " + reason
        rows.append(f"<tr id='source-row-{html.escape(str(numbers[sid]))}'><td style='white-space:nowrap;min-width:32pt'>#{html.escape(str(numbers[sid]))}</td><td>{html.escape(names[sid])}</td>"
                    f"<td>{overlap}</td><td>{html.escape(label)}</td></tr>")
    header = "<tr><th style='width:36pt'>No.</th><th>Paper</th><th>Overlap</th><th>Status</th></tr>"
    tables = ["<table>" + header + "".join(rows[start:start + 12]) + "</table>"
              for start in range(0, len(rows), 12)]
    fully_checked = sum((source_results.get(str(p["id"]), {}).get("status") or p.get("comparison_status")
                         or ("compared-with-limits" if p.get("partial_comparison") else p.get("status"))) == "compared"
                        for p in report.get("papers", []))
    return (f"<h2>Overlap summary / Source key</h2><p>{fully_checked} of {len(report.get('papers', []))} selected papers fully checked."
            " Every selected paper is listed below, including zero overlap and unexamined sources.</p>" + "".join(tables))


def _verify_summary_sources(summary: fitz.Document, numbers: dict[str, int]) -> None:
    expected = Counter(str(number) for number in numbers.values())
    positions = getattr(summary, "_source_row_positions", [])
    opened = Counter(number for number, event in positions if event & 1)
    closed = Counter(number for number, event in positions if event & 2)
    if opened != expected or closed != expected:
        raise PdfReportError("PDF source-key rendering is incomplete or duplicated; the report was not published. Saved comparison evidence is unchanged.")


def generate_pdf(payload: dict, destination: Path) -> dict:
    report, job = payload["report"], payload["job"]
    pages = report.get("manuscript_pages") or job["document"]["pages"]
    text = "\n\n".join(page["text"] for page in pages)
    classified = (report.get("comparison_model") == "classified-v1.1"
                  and isinstance(report.get("classification"), dict)
                  and all(match.get("match_kind") in {"exact", "similar"} for match in report.get("matches", [])
                          if not match.get("excluded_from_score")))
    sequences, failures = _classified_sequences(report, text) if classified else _sequences(report, text)
    original = Path(payload["original"]) if payload.get("original") else None
    mode = "original-pdf"
    warning = ""
    manuscript = fitz.open()
    if original and original.suffix.lower() == ".pdf" and original.is_file():
        expected = job.get("manuscript_sha256")
        actual = hashlib.sha256(original.read_bytes()).hexdigest()
        if expected and expected != actual:
            warning = "The original PDF fingerprint changed. Saved text was typeset instead; original PDF fidelity is unavailable."
            mode = "saved-text"
        else:
            source = fitz.open(original)
            if source.is_encrypted or source.page_count != len(pages) or source.page_count > 250:
                source.close()
                raise PdfReportError("Original PDF cannot be safely matched to the saved page extraction.")
            manuscript.insert_pdf(source, links=False, annots=False)
            source.close()
    else:
        mode = "text-input" if original and original.suffix.lower() == ".txt" else "saved-text"
        if mode == "saved-text":
            warning = "Original PDF is unavailable. This PDF typesets the saved extracted text; it does not reproduce the original layout."
    if mode != "original-pdf":
        manuscript.close()
        paragraphs = "".join("<p>" + html.escape(paragraph).replace("\n", "<br>") + "</p>" for paragraph in text.split("\n\n"))
        manuscript = _typeset(paragraphs)
    numbers = {str(p["id"]): p.get("source_number", i + 1) for i, p in enumerate(report.get("papers", []))}
    names = {str(p["id"]): p.get("filename") or p.get("title", "Source") for p in report.get("papers", [])}
    mapped = annotations = 0
    methods = Counter()
    line_labels = defaultdict(set)
    line_kinds = defaultdict(lambda: defaultdict(set))
    sequence_quads = defaultdict(lambda: defaultdict(list))
    page_glyph_bounds = defaultdict(list)
    offsets, offset = [], 0
    for page in pages:
        offsets.append(offset); offset += len(page["text"]) + 2
    units = [(i, page["text"], offsets[i], [i]) for i, page in enumerate(pages)] if mode == "original-pdf" else [(0, text, 0, list(range(manuscript.page_count)))]
    for logical_page, saved, offset, physical in units:
        glyph_text, glyphs = _glyphs(manuscript, physical)
        for glyph in glyphs:
            if glyph:
                page_glyph_bounds[glyph["page"]].append(glyph["quad"].rect)
        for sequence_index, sequence in enumerate(sequences):
            for a, b in sequence["spans"]:
                start, end = max(a, offset), min(b, offset + len(saved))
                if start >= end:
                    continue
                indices, method = map_region(saved, glyph_text, start - offset, end - offset)
                if not indices or any(glyphs[index] is None for index in indices):
                    failures.append({"sequence": sequence_index + 1, "manuscript_page": logical_page + 1, "start": start, "end": end,
                                     "sources": sorted(sequence["sources"]), "reason": method if not indices else "Matched glyph coordinates unavailable; no guessed placement."})
                    continue
                mapped += 1; methods[method] += 1
                for index in indices:
                    glyph = glyphs[index]
                    sequence_quads[sequence_index][glyph["page"]].append(glyph["quad"])
                    line_labels[(glyph["page"], round(glyph["line"][1] / 4) * 4)].update(sequence["sources"])
                    if classified:
                        for match in sequence["matches"]:
                            line_kinds[(glyph["page"], round(glyph["line"][1] / 4) * 4)][str(match["source_id"])].add(match["match_kind"])
    linked_comments = 0
    continuations = {}
    for sequence_index, by_page in sequence_quads.items():
        sequence = sequences[sequence_index]
        comment_sources = sequence["sources"] | {str(match["source_id"]) for match in sequence.get("overlapping_matches", [])}
        ordered_sources = sorted(comment_sources, key=lambda sid: numbers.get(sid, 0))
        label = ", ".join(f"#{numbers[sid]}" if sid in numbers else "Not recorded" for sid in ordered_sources)
        comments = _sequence_comments(sequence, numbers)
        for page_index, quads in by_page.items():
            # One Highlight annotation holds every marked quad of this sequence
            # on this PDF page. PDF annotations cannot span separate pages.
            pdf_page = manuscript[page_index]
            unique = list({tuple(quad): quad for quad in quads}.values())
            annotation = pdf_page.add_highlight_annot(unique)
            annotation.set_colors(stroke=_color("warning" if sequence.get("visual_kind") == "similar" else "accent"))
            annotation.set_opacity(.24 if sequence.get("visual_kind") == "similar" else .18)
            subject = f"Matched sequence {sequence_index + 1}"
            kinds = {match.get("match_kind") for match in sequence["matches"] + sequence.get("overlapping_matches", [])}
            if kinds <= {"exact", "similar"} and kinds:
                subject += " · " + ("Exact / similar wording" if len(kinds) > 1 else "Exact" if "exact" in kinds else "Similar wording")
            if len(comments) > 1:
                subject += f" · source passage part 1/{len(comments)}"
            title = label
            if classified:
                source_kinds = defaultdict(set)
                for match in sequence["matches"]:
                    source_kinds[str(match["source_id"])].add(match["match_kind"])
                title = "; ".join(f"{'Exact overlap' if kind == 'exact' else 'Similar wording'} · #{numbers.get(sid, sid)}"
                                  for sid in sorted(source_kinds, key=lambda sid: numbers.get(sid, 0))
                                  for kind in sorted(source_kinds[sid]))
            annotation.set_info(title=title, subject=subject, content=comments[0])
            annotation.update()
            annotations += 1
            continuations[(page_index, sequence_index + 1)] = comments[1:]
    omitted_labels = 0
    for (page_index, y), source_ids in line_labels.items():
        page = manuscript[page_index]
        labels = ",".join(str(numbers.get(sid, sid)) for sid in sorted(source_ids, key=lambda sid: numbers.get(sid, 0)))
        if classified:
            labels = " ".join("/".join("E" if kind == "exact" else "S" for kind in sorted(line_kinds[(page_index, y)][sid]))
                              + str(numbers.get(sid, sid)) for sid in sorted(source_ids, key=lambda sid: numbers.get(sid, 0)))
        rect = fitz.Rect(3, max(0, y), 34, min(page.rect.height, y + 11))
        if len(labels) > 14 or any(rect.intersects(bound) for bound in page_glyph_bounds[page_index]):
            omitted_labels += 1
            continue
        page.insert_textbox(rect, labels, fontsize=6.5, fontname="helv", color=_color("accent"), align=fitz.TEXT_ALIGN_RIGHT)
    outcome = result_state(report)
    metrics = report.get("metrics", {})
    denominator = metrics.get("score_denominator_words", metrics.get("eligible_words", 0))
    body = f"<h1>Paper Overlap Detector</h1><p><b>{html.escape(str(job.get('filename') or job.get('title', 'Your paper')))}</b></p>"
    if classified:
        counts = (report.get("classification") or {}).get("metrics") or {}
        body += "<p class='warn'>Experimental exact + similar wording model. Scores may differ; accuracy and vendor equivalence are not established.</p>"
        body += (f"<p><b>E · Exact overlap</b>: {int(counts.get('exact_words', 0))} words · "
                 f"<b>S · Similar wording</b> only: {int(counts.get('similar_only_words', 0))} words · "
                 f"Combined: {int(counts.get('combined_words', 0))} unique words.</p>")
        body += "<p class='muted'>Exact: contiguous equal normalized words. Similar: shared wording with bounded edits or reordering. Exact takes visual precedence; comments retain source alternatives. E/S marks remain usable in grayscale.</p>"
    body += _summary_table(report, numbers, names)
    basis = {"all-submitted-word-units": "total submitted word units",
             "abstract-onward-word-units": "words from the Abstract onward"}.get(metrics.get("score_basis"), "eligible words (saved legacy basis)")
    body += f"<p class='muted'>{metrics.get('overlapping_words', 0)} unique matching words / {denominator} {basis}. Source percentages are not additive. Algorithm {html.escape(str(report.get('algorithm_version', 'legacy')))}.</p>"
    scope = (report.get("settings") or {}).get("manuscript_scope") or {}
    if scope.get("applied") == "abstract-onward":
        body += f"<p class='muted'>Analysis starts at the Abstract heading on page {html.escape(str(scope.get('start_page')))}; {html.escape(str(metrics.get('front_matter_words', 0)))} preceding front-matter words excluded. Original PDF pages are preserved.</p>"
    elif scope.get("requested") == "abstract-onward":
        body += "<p class='warn'>Abstract heading not detected; the whole manuscript was analyzed (front matter was not excluded).</p>"
    if outcome["state"] in {"all_excluded", "unscorable", "not_compared"}:
        body += f"<p class='warn'>{html.escape(outcome['explanation'])}</p>"
    elif outcome["score_kind"] == "lower_bound":
        body += "<p class='warn'>Partial comparison: overlap is a lower bound; not all source passages were checked.</p>"
    if report.get("comparison_model") == "experimental-ordered":
        body += "<p class='warn'>Experimental comparison model: accuracy and vendor equivalence are not established.</p>"
    if report.get("comparison_model") == "classified-v1.1" and not classified:
        body += "<p class='warn'>Exact/similar classification data is unavailable in this saved report. Existing evidence is shown without inferred match-type labels.</p>"
    if classified:
        counts = (report.get("classification") or {}).get("metrics") or {}
        if counts.get("all_sources_fully_checked"):
            body += f"<p>{int(counts.get('unmatched_words', 0))} eligible words had no match found by this model in the checked sources—not a finding of originality.</p>"
        else:
            body += "<p>Unmarked eligible text is not fully checked because the source coverage is incomplete; it must not be described as unmatched or original.</p>"
    body += "<p>Original manuscript pages follow." if mode == "original-pdf" else "<p>The following pages are typeset from saved text."
    body += " Highlights reference the numbered papers above. For a readable side panel, open the PDF in Adobe Acrobat Reader and open Comments. Browser PDF viewers may not show comments or may clip popups. Long passages continue in linked replies. Selected comparison papers only; not a plagiarism verdict.</p>"
    if warning:
        body += "<p class='warn'>" + html.escape(warning) + "</p>"
    if failures:
        body += f"<p class='warn'>{len(failures)} highlight range(s) could not be mapped uniquely. They were NOT placed at guessed locations. Saved evidence and mapping reasons are available in JSON; the comparison score was not changed.</p>"
        body += "<p>Unmapped manuscript pages: " + html.escape(", ".join(sorted({str(item.get("manuscript_page", "unknown")) for item in failures}))) + ".</p>"
    if omitted_labels:
        body += f"<p class='muted'>{omitted_labels} lines had no unobstructed margin for a number. Their PDF highlight notes still identify the papers.</p>"
    summary = _typeset(body)
    try:
        _verify_summary_sources(summary, numbers)
    except PdfReportError:
        summary.close()
        manuscript.close()
        raise
    if classified:
        for phrase, color, opacity in (("E · Exact overlap", "accent", .18), ("S · Similar wording", "warning", .24)):
            for page in summary:
                found = page.search_for(phrase)
                if found:
                    page.draw_rect(found[0], color=None, fill=_color(color), fill_opacity=opacity, overlay=False)
                    break
    output = fitz.open()
    output.insert_pdf(summary)
    prefix_pages = summary.page_count
    output.insert_pdf(manuscript, links=False, annots=True)
    for output_page_index, page in enumerate(output):
        output.xref_set_key(page.xref, "AA", "null")
        # Page copying retains annotation contents/quads but not all relationship
        # objects. Create Popup/IRT links in the final document's xref namespace.
        for annotation in list(page.annots() or []):
            if annotation.type[1] == "Highlight":
                _set_comment_popup(page, annotation)
                sequence = re.search(r"Matched sequence (\d+)", annotation.info.get("subject", ""))
                if sequence:
                    extra = continuations.get((output_page_index - prefix_pages, int(sequence.group(1))), [])
                    for part, comment in enumerate(extra, 2):
                        position = _reply_position(page)
                        reply = page.add_text_annot(position if position is not None else annotation.rect.tl, comment)
                        if position is None:
                            # Keep the comment in the thread without covering manuscript text.
                            reply.set_flags(reply.flags | fitz.PDF_ANNOT_IS_NO_VIEW)
                        reply.set_info(title=annotation.info["title"],
                                       subject=f"Source context continuation {part}/{len(extra) + 1}")
                        output.xref_set_key(reply.xref, "IRT", f"{annotation.xref} 0 R")
                        output.xref_set_key(reply.xref, "RT", "/R")
                        _set_comment_popup(page, reply)
                        linked_comments += 1
    output.set_metadata({"title": "Paper Overlap Detector report", "author": "Paper Overlap Detector", "creator": f"Paper Overlap Detector PDF renderer {PDF_RENDERER_VERSION}"})
    excluded_matches = [match for match in report.get("matches", []) if match.get("excluded_from_score")]
    if excluded_matches:
        note_page = output[0]
        note = note_page.add_text_annot(fitz.Point(570, 36), "Excluded evidence remains available in the accompanying JSON. It is not marked as included overlap in this PDF.")
        note.set_info(title="Excluded evidence", content="\n\n".join(
            f"Source {numbers.get(str(match['source_id']), '?')} · manuscript page {match['manuscript'].get('page', '?')}: "
            + match["manuscript"].get("text", "")[:1000] for match in excluded_matches[:50]
        ) + ("\nAdditional excluded evidence is retained in JSON." if len(excluded_matches) > 50 else ""))
        note.update()
    output.set_toc([[1, "Summary and source key", 1], [1, "Annotated manuscript", prefix_pages + 1]])
    output.save(destination, garbage=4, deflate=True)
    manifest = {"renderer_version": PDF_RENDERER_VERSION, "render_mode": mode,
                "original_manuscript_pages": len(pages), "pdf_pages": output.page_count,
                "summary_pages": prefix_pages, "mapped_regions": mapped, "unmapped_regions": len(failures),
                "mapping_methods": dict(methods), "annotations": annotations, "unplaced_margin_labels": omitted_labels,
                "sequence_groups": len(sequences), "linked_source_comments": linked_comments,
                "failures": failures, "warning": warning,
                "comparison_recomputed": False}
    output.close(); summary.close(); manuscript.close()
    return manifest


def cached_pdf(folder: Path, job: dict, report: dict, report_bytes: bytes) -> tuple[Path, dict]:
    original_name = job.get("manuscript_file")
    if original_name and Path(original_name).name != original_name:
        raise PdfReportError("Unsafe stored manuscript filename.")
    original = folder / original_name if original_name else None
    original_hash = hashlib.sha256(original.read_bytes()).hexdigest() if original and original.is_file() else "missing"
    key = hashlib.sha256(report_bytes + json.dumps({
        "renderer": PDF_RENDERER_VERSION, "code": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "library": fitz.VersionBind, "theme": hashlib.sha256(_THEME.encode()).hexdigest(),
        "original": original_hash, "pages": job["document"]["pages"],
    }, sort_keys=True).encode()).hexdigest()
    pdf_path, metadata_path = folder / f"report-pdf-{key[:24]}.pdf", folder / f"report-pdf-{key[:24]}.json"
    with _LOCK:
        if pdf_path.exists() and metadata_path.exists():
            with pdf_path.open("rb") as stream:
                valid = stream.read(5) == b"%PDF-"
            if valid:
                return pdf_path, json.loads(metadata_path.read_text(encoding="utf-8"))
            raise PdfReportError("The cached PDF is invalid. Original comparison data was preserved.")
        request = folder / f"pdf-request-{uuid4()}.json"
        temporary = folder / f"pdf-output-{uuid4()}.pdf"
        temporary_metadata = temporary.with_suffix(".json")
        try:
            request.write_text(json.dumps({"report": report, "job": job, "original": str(original) if original and original.exists() else None}), encoding="utf-8")
            os.chmod(request, 0o600)
            result = subprocess.run([sys.executable, "-m", "buna.pdf_worker", str(request), str(temporary)],
                                    capture_output=True, timeout=120, check=False)
            if result.returncode or not temporary.exists():
                message = result.stdout.decode("utf-8", errors="replace")[:500]
                raise PdfReportError(message or "PDF renderer failed or reached a resource limit.")
            metadata = json.loads(temporary_metadata.read_text(encoding="utf-8"))
            os.chmod(temporary, 0o600); os.chmod(temporary_metadata, 0o600)
            temporary.replace(pdf_path); temporary_metadata.replace(metadata_path)
            return pdf_path, metadata
        except subprocess.TimeoutExpired as exc:
            raise PdfReportError("PDF generation exceeded its 120-second safety limit.") from exc
        finally:
            request.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            temporary_metadata.unlink(missing_ok=True)
