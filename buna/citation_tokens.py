"""Conservative citation masks in original token coordinates; no token deletion."""
import re

from buna.comparison import _CITATION, _mask

_NAME = r"[A-ZÀ-ÖØ-Þ][^\W\d_]+(?:[-’'][^\W\d_]+)*"
_NARRATIVE = re.compile(
    rf"\b{_NAME}(?:(?:\s*,\s*(?:(?:and|&)\s+)?|\s+(?:and|&)\s+){_NAME})*(?:\s+et\s+al\.)?"
    r"\s*\((?:19|20)\d{2}[a-z]?(?:\s*[,;]\s*(?:19|20)\d{2}[a-z]?)*\)"
)
_PARENTHETICAL = re.compile(r"\([^\W\d_][^()\n]{0,200}\b(?:19|20)\d{2}[a-z]?\b[^()\n]{0,80}\)")


def citation_mask(text, tokens):
    intervals = sorted([m.span() for m in _CITATION.finditer(text)]
                       + [m.span() for m in _NARRATIVE.finditer(text)]
                       + [m.span() for m in _PARENTHETICAL.finditer(text)])
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return _mask(tokens, merged)
