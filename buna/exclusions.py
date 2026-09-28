"""Auditable quotation recognition; not a reproduction of vendor ML detection."""
from __future__ import annotations

import re

PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’", "«": "»", "»": "«",
         "„": "“", "《": "》", "〈": "〉", "『": "』"}
MAX_QUOTE_CHARACTERS = 10_000


def quotation_intervals(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    stack: list[tuple[str, str, int]] = []
    intervals, uncertain_positions = [], []
    delimiters = set(PAIRS) | set(PAIRS.values())
    for index, char in enumerate(text):
        if char not in delimiters:
            continue
        previous = text[index - 1] if index else ""
        following = text[index + 1] if index + 1 < len(text) else ""
        # Apostrophes inside words are not quotation marks, even inside a quote.
        if char in {"'", "’"} and previous.isalnum() and following.isalnum():
            continue
        can_close = bool(previous and not previous.isspace() and (not following or not following.isalnum()))
        if stack and char == stack[-1][1] and (char not in {'"', "'"} or can_close):
            _, _, start = stack.pop()
            if index - start <= MAX_QUOTE_CHARACTERS:
                intervals.append((start, index + 1))
            else:
                uncertain_positions.extend([start, index])
            continue
        # Possessives and measurement marks do not open a new quotation.
        if char in {"'", "’"} and previous.isalnum():
            continue
        if char == '"' and previous.isdigit():
            continue
        can_open = bool(following and not following.isspace() and (not previous or not previous.isalnum()))
        if char in PAIRS and (char not in {'"', "'"} or can_open):
            stack.append((char, PAIRS[char], index))
        else:
            uncertain_positions.append(index)
    uncertain_positions.extend(start for _, _, start in stack)
    # Explicit textual blockquote syntax is our retained approximation; PDF
    # indentation alone is never used as a quote exclusion.
    intervals.extend((m.start(), m.end()) for m in re.finditer(r"(?m:^[ \t]*>[^\n]+)", text))
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    uncertain = []
    for position in uncertain_positions:
        start = text.rfind("\n\n", max(0, position - MAX_QUOTE_CHARACTERS), position)
        end = text.find("\n\n", position, position + MAX_QUOTE_CHARACTERS)
        uncertain.append((start + 2 if start >= 0 else max(0, position - MAX_QUOTE_CHARACTERS),
                          end if end >= 0 else min(len(text), position + MAX_QUOTE_CHARACTERS)))
    uncertain.extend((m.start(), m.end()) for m in re.finditer(r"(?m:^[ \t]{4,}[^\n]+)", text)
                     if not any(a <= m.start() and m.end() <= b for a, b in merged))
    return merged, sorted(uncertain)
