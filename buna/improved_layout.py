"""Conservative page-edge removal for the improvedEng logical token view only."""
import re
from collections import defaultdict

from buna.comparison import _mask
from buna.documents import _is_heading

LAYOUT_VERSION = "repeated-numbered-page-edge-v1"


def running_headers(document, ledger):
    """Require repeated short edge text plus a page-correct counter.

    Extraction has no geometry. Unnumbered/ambiguous repeats remain intact with
    a warning; ordinary headings, tables and repeated prose are not inferred away.
    """
    groups = defaultdict(list)
    offset = 0
    pages = document.get("pages", [])
    if "\n\n".join(page.get("text", "") for page in pages) != document.get("text", ""):
        return [False] * len(ledger), ["Running-header detection unavailable: page text does not match the original ledger."]
    for page_index, page in enumerate(pages):
        lines, local = [], 0
        for line in page.get("text", "").splitlines(keepends=True):
            if line.strip():
                lines.append((offset + local, offset + local + len(line.rstrip()), line.strip()))
            local += len(line)
        # At least two body lines prevents single-line page content being removed.
        if len(lines) >= 3:
            candidates = [("top", lines[0]), ("bottom", lines[-1])]
            if len(lines) >= 4:
                for edge, pair in (("top", lines[:2]), ("bottom", lines[-2:])):
                    if any(item[2].isdigit() for item in pair):
                        candidates.append((edge, (pair[0][0], pair[1][1], " ".join(item[2] for item in pair))))
            for edge, (start, end, line) in candidates:
                numbered = re.fullmatch(r"(?:(\d{1,3})\s+)?(.+?)(?:\s+(\d{1,3}))?", line)
                if not numbered:
                    continue
                first, identity, last = numbered.groups()
                counter = first or last
                words = re.findall(r"[^\W\d_]+", identity)
                if (not 2 <= len(words) <= 12 or len(line) > 120 or _is_heading(identity)
                        or re.search(r"[.!?;:=\t]|\d", identity)):
                    continue
                key = (edge, " ".join(identity.casefold().split()))
                groups[key].append((page_index, start, end, counter is not None
                                    and int(counter) == page.get("number")))
        offset += len(page.get("text", "")) + 2
    removed, ambiguous = [], False
    for occurrences in groups.values():
        if len(occurrences) < 2:
            continue
        numbered_pages = {item[0] for item in occurrences if item[3]}
        if len(numbered_pages) >= 2:
            # Only confirmed occurrences; do not strip the first-page title.
            removed.extend((start, end) for _, start, end, confirmed in occurrences if confirmed)
        else:
            ambiguous = True
    warnings = (["Ambiguous repeated page-edge text retained: extraction has no geometry and no repeated page-correct counter."]
                if ambiguous else [])
    merged = []
    for start, end in sorted(removed):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    return _mask(ledger, merged), warnings
