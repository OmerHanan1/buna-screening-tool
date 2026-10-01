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


# Opt-in: old engines and saved-report evaluation keep their historical grammar.
APA_VERSION = "apa-author-year-original-offsets-v2"
_INITIALS = r"(?:[A-Z]\.\s*){1,3}"
_PARTICLE = r"(?:(?:[Vv]an(?:\s+der)?|[Vv]on|[Dd]e(?:\s+la)?|[Dd]el|[Dd]a)\s+)?"
_AUTHOR = rf"(?:{_INITIALS})?{_PARTICLE}{_NAME}(?:,\s*{_INITIALS})?"
_AUTHORS = rf"{_AUTHOR}(?:(?:\s*,\s*(?:(?:and|&)\s+)?|\s+(?:and|&)\s+){_AUTHOR}){{0,9}}(?:\s+et\s+al\.?)?"
_YEARS = r"(?:18|19|20)\d{2}[a-z]?(?:\s*,\s*(?:(?:18|19|20)\d{2}[a-z]?|[a-z])){0,9}"
_APA_NARRATIVE = re.compile(rf"(?<!\w){_AUTHORS}\s*\({_YEARS}(?:\s*;\s*{_YEARS}){{0,9}}\)")
_APA_INLINE = re.compile(rf"(?<!\w){_AUTHORS}\s*,\s*{_YEARS}\b")
_APA_ITEM = re.compile(rf"\s*(?:(?:see|also|e\.g\.,?|cf\.)\s+)*{_AUTHORS}\s*,?\s+{_YEARS}\s*")
_APA_PARENS = re.compile(r"\([^()]{1,1000}\)")
_NUMERIC_CITATION = re.compile(r"\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]")
_NON_AUTHOR = frozenset(("study studies experiment experiments table figure anova ancova manova "
                         "year years sample samples participants results model test tests").split())


def improved_citation_mask(text, tokens, removed=None):
    """Recognize complete APA author evidence, never a year/et-al fragment alone."""
    if removed is not None and any(removed):
        # Spaces preserve every original character offset while making removed
        # layout words transparent to multiline citation syntax.
        parts, cursor = [], 0
        for (_, start, end), excluded in zip(tokens, removed):
            if excluded:
                parts.extend((text[cursor:start], " " * (end - start)))
                cursor = end
        parts.append(text[cursor:])
        text = "".join(parts)
    intervals = [m.span() for m in _NUMERIC_CITATION.finditer(text)]
    for grammar in (_APA_NARRATIVE, _APA_INLINE):
        for match in grammar.finditer(text):
            first = re.match(r"\w+", match.group()).group().casefold()
            if first not in _NON_AUTHOR:
                intervals.append(match.span())
    for match in _APA_PARENS.finditer(text):
        items = match.group()[1:-1].split(";")
        if all(_APA_ITEM.fullmatch(item) for item in items):
            first_words = [re.search(r"\w+", item).group().casefold() for item in items]
            if not any(word in _NON_AUTHOR for word in first_words):
                intervals.append(match.span())
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    mask = _mask(tokens, merged)
    return [c and not r for c, r in zip(mask, removed)] if removed is not None else mask
