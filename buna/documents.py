"""Local, bounded text extraction. Offsets always address the returned ``text``."""

from __future__ import annotations

import re
from pathlib import Path

from buna.exclusions import quotation_intervals

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 250
MAX_CHARACTERS = 1_000_000
SOURCE_MAX_BYTES = 32 * 1024 * 1024
SOURCE_MAX_PAGES = 600
SOURCE_MAX_CHARACTERS = 2_000_000
PARSER_SECONDS = 35
PARSER_CPU_SECONDS = 25
PARSER_MEMORY_BYTES = 768 * 1024 * 1024
STRUCTURE_VERSION = 3


class ExtractionError(ValueError):
    """The supplied document cannot be safely extracted."""


_REFERENCE_HEADING = re.compile(
    r"^(?:\d+[.)]?\s+)?(?:references|bibliography|works cited|cited works|literature cited|"
    r"reference list|list of references|selected references|references and notes|endnotes|"
    r"annotated bibliography|selective bibliography|bibliography cited|"
    r"bibliographical references and notes|cited references|"
    r"references? cited|references? and notes?|reference notes|"
    r"references and further reading|references and links|references index|"
    r"references list|references section)\s*:?\s*$",
    re.I,
)
_COMMON_HEADING = re.compile(
    r"^(?:(?:\d+(?:\.\d+)*[.)]?)\s+)?"
    r"(?:abstract|introduction|background|methods?|materials and methods|methodology|"
    r"results|discussion|conclusions?|limitations|acknowledg(?:e)?ments?|appendix"
    r"(?:\s+[A-Z])?|supplementary material)\s*:?\s*$",
    re.I,
)
_NUMBERED_HEADING = re.compile(r"^\d+(?:\.\d+)*[.)]?\s+[A-Z][^\n.!?]{1,90}$")
_REFERENCE_END_HEADING = re.compile(
    r"^(?:\d+[.)]?\s+)?(?:appendix|appendices|glossary|tables?|figures?|charts?|exhibits|"
    r"acknowledg(?:e)?ments?)(?:\s+(?:[A-Z]|\d+(?:\.\d+)*))?(?:\s*:\s*[^\n]{1,80})?\s*$",
    re.I,
)
_CHAPTER_HEADING = re.compile(r"^chapter\s+(?:\d+|[ivxlcdm]+)(?:\s*:\s*[^\n]{1,80})?\s*$", re.I)
_REFERENCE_START = re.compile(r"^\s*(?:\[\d+\]|\d+[.)])\s+")
_DOI = re.compile(r"\b10\.\d{4,9}/[^\s<>\"]+", re.I)


_ABSTRACT_ALONE = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?\s+)?abstract\s*[:.]?$", re.I)
_ABSTRACT_INLINE = re.compile(r"^(?:\d+(?:\.\d+)*[.)]?\s+)?abstract\s*(?::|\.|—|–|-)\s*\S", re.I)
_WORDS = re.compile(r"[^\W\d_]+", re.UNICODE)


def abstract_start(document: dict) -> dict:
    """Locate the first structural Abstract heading in the manuscript text.

    A heading alone on its line must be followed by a paragraph line of at least six
    words (TOC entries are followed by another short heading); an inline heading needs
    a separator and text. Page numbers, dot leaders, quotes and anything after the
    first recognized bibliography heading are not headings. Offsets address ``text``.
    """
    text = document.get("text", "")
    references = [s["start"] for s in document.get("segments", [])
                  if s.get("kind") in ("reference", "bibliography") or _REFERENCE_HEADING.fullmatch(s.get("section", ""))]
    limit = min(references) if references else len(text)
    quoted = quotation_intervals(text)[0]
    lines, offset = [], 0
    for line in text.split("\n"):
        lines.append((offset, line)); offset += len(line) + 1
    for index, (start, line) in enumerate(lines):
        stripped = line.strip()
        begin = start + len(line) - len(line.lstrip())
        if begin >= limit:
            break
        if any(qa <= begin < qb for qa, qb in quoted):
            continue
        inline = bool(_ABSTRACT_INLINE.match(stripped)) and len(_WORDS.findall(stripped)) >= 7
        alone = bool(_ABSTRACT_ALONE.match(stripped))
        if alone:
            following = next((l.strip() for _, l in lines[index + 1:] if l.strip()), "")
            alone = len(_WORDS.findall(following)) >= 6 and not re.search(r"(?:\.{3,}|\s)\d+$", following)
        if inline or alone:
            page, cursor = None, 0
            for item in document.get("pages", []):
                if cursor <= begin < cursor + len(item.get("text", "")) + 2:
                    page = item.get("number"); break
                cursor += len(item.get("text", "")) + 2
            return {"start_offset": begin, "start_page": page, "heading_text": stripped[:80]}
    return {"start_offset": None, "start_page": None, "heading_text": None}


def _is_heading(line: str) -> bool:
    return bool(_REFERENCE_HEADING.fullmatch(line) or _COMMON_HEADING.fullmatch(line)
                or _REFERENCE_END_HEADING.fullmatch(line) or _CHAPTER_HEADING.fullmatch(line)
                or _NUMBERED_HEADING.fullmatch(line))


def _structure(pages: list[dict], title: str, warnings: list[str]) -> dict:
    text = "\n\n".join(page["text"] for page in pages)
    segments: list[dict] = []
    references: list[dict] = []
    section = "Unspecified"
    in_references = False
    page_offset = 0
    pending: list[tuple[int, int]] = []
    pending_kind = "body"
    pending_page = 1
    pending_section = section

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        start, end = pending[0][0], pending[-1][1]
        raw = text[start:end]
        segments.append({
            "id": f"segment-{len(segments) + 1}", "page": pending_page,
            "section": pending_section, "kind": pending_kind,
            "text": raw, "start": start, "end": end,
        })
        if pending_kind == "reference":
            reference = {"id": f"reference-{len(references) + 1}", "raw": raw}
            doi = _DOI.search(raw)
            if doi:
                reference["doi"] = doi.group().rstrip(".,;)]}")
            references.append(reference)
        pending = []

    for page in pages:
        local_offset = 0
        for line in page["text"].splitlines(keepends=True):
            stripped = line.strip()
            start = page_offset + local_offset + len(line) - len(line.lstrip())
            end = page_offset + local_offset + len(line.rstrip())
            local_offset += len(line)
            if not stripped:
                flush()
                continue
            heading = _is_heading(stripped) and (
                not in_references or not _REFERENCE_START.match(stripped)
                or bool(_COMMON_HEADING.fullmatch(stripped))
                or bool(_REFERENCE_HEADING.fullmatch(stripped))
                or bool(_REFERENCE_END_HEADING.fullmatch(stripped))
                or bool(_CHAPTER_HEADING.fullmatch(stripped))
            )
            if heading:
                flush()
                section = stripped
                if _REFERENCE_HEADING.fullmatch(stripped):
                    in_references = True
                elif in_references:
                    in_references = False
                segments.append({
                    "id": f"segment-{len(segments) + 1}", "page": page["number"],
                    "section": section, "kind": "heading", "text": text[start:end],
                    "start": start, "end": end,
                })
                continue
            if in_references and _REFERENCE_START.match(stripped):
                flush()
            if not pending:
                pending_page = page["number"]
                pending_section = section
                pending_kind = "reference" if in_references else "body"
            pending.append((start, end))
        flush()
        page_offset += len(page["text"]) + 2
    if references:
        warnings.append(
            "References are conservative raw-text candidates, not verified bibliographic records; "
            "review and edit them before lookup. Wrapped or unnumbered entries may be merged."
        )
    else:
        warnings.append("No explicit reference section was recognized; references may require manual entry.")
    return {"text": text, "pages": pages, "segments": segments,
            "references": references, "warnings": warnings, "title": title,
            "structure_version": STRUCTURE_VERSION}


def current_structure(document: dict) -> dict:
    """Apply current heading rules to saved extraction only for an explicit new run.

    Original files and old reports are untouched; no PDF parsing or network is needed.
    """
    if document.get("structure_version") == STRUCTURE_VERSION:
        return document
    rebuilt = _structure(document["pages"], document.get("title", "Document"), list(document.get("warnings", [])))
    rebuilt["warnings"] = list(dict.fromkeys(rebuilt["warnings"]))
    return rebuilt


def extract_document(path: Path, *, profile: str = "manuscript") -> dict:
    """Extract PDF or UTF-8/UTF-16 TXT without OCR, network access, or inferred text."""
    path = Path(path)
    if profile not in {"manuscript", "source"}:
        raise ExtractionError("Unknown extraction profile.")
    max_bytes, max_pages, max_characters = (
        (SOURCE_MAX_BYTES, SOURCE_MAX_PAGES, SOURCE_MAX_CHARACTERS) if profile == "source"
        else (MAX_BYTES, MAX_PAGES, MAX_CHARACTERS)
    )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ExtractionError(f"Cannot read document: {exc}") from exc
    if size > max_bytes:
        raise ExtractionError(f"Document exceeds the {max_bytes // (1024 * 1024)} MiB {profile} extraction limit.")
    if not size:
        raise ExtractionError("Document is empty.")
    warnings: list[str] = []
    pages: list[dict] = []
    title = path.stem
    try:
        if path.suffix.lower() == ".txt":
            data = path.read_bytes()
            encoding = "utf-16" if data[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"
            text = data.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
            if "\x00" in text:
                raise ExtractionError("TXT contains binary NUL characters.")
            if len(text) > max_characters:
                raise ExtractionError(f"Document exceeds the {max_characters:,}-character {profile} extraction limit.")
            pages = [{"number": 1, "text": text}]
            warnings.append("TXT has synthetic page 1; original pagination is unavailable.")
        elif path.suffix.lower() == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(path)
            if reader.is_encrypted:
                raise ExtractionError("Encrypted PDFs are not supported; provide an unlocked local copy.")
            if len(reader.pages) > max_pages:
                raise ExtractionError(f"PDF exceeds the {max_pages}-page {profile} extraction limit.")
            total = 0
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                total += len(text) + (2 if number > 1 else 0)
                if total > max_characters:
                    raise ExtractionError(f"Document exceeds the {max_characters:,}-character {profile} extraction limit.")
                pages.append({"number": number, "text": text})
                if not text.strip():
                    warnings.append(
                        f"Page {number} has no extractable text (blank or scanned); it was not OCR processed."
                    )
            metadata = reader.metadata
            if metadata and metadata.title:
                title = str(metadata.title)[:500]
            warnings.append(
                "PDF reading order, columns, ligatures, tables and line-break hyphenation may affect "
                "extraction. No OCR or layout reconstruction is performed."
            )
        else:
            raise ExtractionError("Unsupported document format; provide a PDF or TXT file.")
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Malformed or unextractable document: {exc}") from exc
    if not pages or not any(re.search(r"[^\W_]", page["text"], re.UNICODE) for page in pages):
        raise ExtractionError("Document has no extractable text; scanned PDFs require OCR before upload.")
    if title == path.stem:
        title = next((line.strip()[:500] for page in pages for line in page["text"].splitlines()
                      if line.strip()), title)
    result = _structure(pages, title, warnings)
    result["extraction"] = {
        "profile": profile + "-v1", "parser": "pypdf" if path.suffix.lower() == ".pdf" else "plain-text",
        "pages_extracted": len(pages), "characters_extracted": len(result["text"]), "truncated": False,
    }
    return result
