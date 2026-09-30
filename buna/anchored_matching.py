"""Precision-first lexical hypotheses for improvedEng-v2, not vendor parameters."""
from __future__ import annotations

from collections import Counter, defaultdict
import re

from buna.classified import STOPWORDS, _regions
from buna.comparison import _CITATION, _mask

PROFILE = {
    "anchor_content_words": 4,
    "minimum_qualifying_matches": 9,
    "max_gap_per_side": 2,
    "max_total_gap_per_side": 6,
    "max_gap_events": 3,
    "local_window_tokens": 12,
    "local_density": 0.70,
    "global_density": 0.70,
    "max_citation_interruption_tokens": 16,
    "common_term_min_count": 3,
    "common_term_fraction": 0.005,
    "minimum_uncommon_anchor_words": 2,
    "minimum_informative_matches_per_window": 2,
}
_NAME = r"[A-ZÀ-ÖØ-Þ][^\W\d_]+(?:[-’'][^\W\d_]+)*"
_NARRATIVE = re.compile(
    rf"\b{_NAME}(?:(?:\s*,\s*(?:(?:and|&)\s+)?|\s+(?:and|&)\s+){_NAME})*(?:\s+et\s+al\.)?"
    r"\s*\((?:19|20)\d{2}[a-z]?(?:\s*[,;]\s*(?:19|20)\d{2}[a-z]?)*\)"
)
_PARENTHETICAL = re.compile(r"\([^\W\d_][^()\n]{0,200}\b(?:19|20)\d{2}[a-z]?\b[^()\n]{0,80}\)")


def citation_mask(text, tokens):
    """Only explicit numeric/author-year syntax; ordinary names remain prose."""
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


def content(word):
    return word.isalpha() and len(word) >= 3 and word not in STOPWORDS


def common_words(words, cited):
    prose = [w for w, c in zip(words, cited) if not c]
    counts = Counter(prose)
    threshold = max(PROFILE["common_term_min_count"], len(prose) * PROFILE["common_term_fraction"])
    return {w for w, count in counts.items() if count >= threshold}


def continuity(path, m_rank, s_rank, informative=None):
    """Ranks omit citations, not ordinary stopwords, numbers or unmatched prose."""
    count = len(path)
    spans = [r[path[-1][side]] - r[path[0][side]] + 1 for side, r in enumerate((m_rank, s_rank))]
    gaps = [(m_rank[b[0]] - m_rank[a[0]] - 1, s_rank[b[1]] - s_rank[a[1]] - 1)
            for a, b in zip(path, path[1:])]
    totals = [sum(g[side] for g in gaps) for side in (0, 1)]
    maximum = max((max(g) for g in gaps), default=0)
    events = sum(bool(a or b) for a, b in gaps)
    local, informative_minima = [], []
    for side, rank in enumerate((m_rank, s_rank)):
        span = spans[side]
        width = min(PROFILE["local_window_tokens"], span)
        positions = {rank[p[side]] - rank[path[0][side]] for p in path}
        informative_positions = ({rank[p[side]] - rank[path[0][side]] for p in path if informative[side][p[side]]}
                                 if informative is not None else positions)
        # Every moving window, not disjoint chunks or just the newest suffix.
        hits = sum(i in positions for i in range(width))
        minimum = hits
        informative_hits = sum(i in informative_positions for i in range(width))
        informative_minimum = informative_hits
        for end in range(width, span):
            hits += int(end in positions) - int(end - width in positions)
            minimum = min(minimum, hits)
            informative_hits += int(end in informative_positions) - int(end - width in informative_positions)
            informative_minimum = min(informative_minimum, informative_hits)
        local.append(minimum / width)
        informative_minima.append(informative_minimum)
    density = [count / span for span in spans]
    reason = None
    if maximum > PROFILE["max_gap_per_side"]:
        reason = "maximum-gap"
    elif max(totals) > PROFILE["max_total_gap_per_side"]:
        reason = "cumulative-gap-budget"
    elif events > PROFILE["max_gap_events"]:
        reason = "gap-event-budget"
    elif min(local) + 1e-12 < PROFILE["local_density"]:
        reason = "local-density"
    elif min(density) + 1e-12 < PROFILE["global_density"]:
        reason = "global-density"
    elif informative is not None and min(informative_minima) < PROFILE["minimum_informative_matches_per_window"]:
        reason = "generic-bridge"
    return {
        "qualifying_matched_words": count,
        "number_of_gaps": events, "maximum_gap": maximum,
        "total_gap_words": sum(totals), "gap_words_per_side": totals,
        "local_minimum_density": min(local), "local_density_per_side": local,
        "global_density": min(density), "global_density_per_side": density,
        "local_minimum_informative_matches": informative_minima,
    }, reason


def _ranks(cited):
    result, rank = [], 0
    for value in cited:
        result.append(rank)
        rank += not value
    return result


def _longest(path):
    longest = run = 0
    previous = None
    for a, b in path:
        run = run + 1 if previous == (a - 1, b - 1) else 1
        longest = max(longest, run)
        previous = (a, b)
    return longest


def anchored_paths(mw, sw, m_ok, s_ok, m_cited, s_cited, check, limit,
                   *, working_bytes=128 * 1024 * 1024):
    """Extend every strong seed independently; stop a branch at weak continuity.

    The asymmetric traversal (left then right) is deterministic. Acceptance is
    defined by continuity at every extension step, not an optimal edit score.
    Alternatives inside the declared budgets are not silently capped.
    """
    mr, sr = _regions(m_ok), _regions(s_ok)
    m_rank, s_rank = _ranks(m_cited), _ranks(s_cited)
    m_common, s_common = common_words(mw, m_cited), common_words(sw, s_cited)
    generic = m_common & s_common
    informative = ([content(w) and w not in generic for w in mw],
                   [content(w) and w not in generic for w in sw])
    table = defaultdict(list)
    memory = 0
    for j in range(len(sw) - 3):
        check()
        if (sr[j] == sr[j + 3] and all(s_ok[j:j + 4]) and not any(s_cited[j:j + 4])
                and all(content(w) for w in sw[j:j + 4])):
            memory += 448
            if memory > working_bytes:
                raise limit("index-memory-limit", "Strong-anchor index exceeded its working-memory budget.")
            table[tuple(sw[j:j + 4])].append(j)
    visited_seeds = set()
    for i in range(len(mw) - 3):
        check()
        seed_words = mw[i:i + 4]
        if not (mr[i] == mr[i + 3] and all(m_ok[i:i + 4]) and not any(m_cited[i:i + 4])
                and all(content(w) for w in seed_words)):
            continue
        if sum(w not in generic for w in seed_words) < PROFILE["minimum_uncommon_anchor_words"]:
            continue
        for j in table.get(tuple(seed_words), ()):
            check()
            if (i, j) in visited_seeds:
                continue
            seed = tuple((i + n, j + n) for n in range(4))
            left_m, left_s, right_m, right_s = i, j, i + 4, j + 4
            while (left_m > 0 and left_s > 0 and mr[left_m - 1] == mr[i] and sr[left_s - 1] == sr[j]
                   and m_ok[left_m - 1] and s_ok[left_s - 1] and not m_cited[left_m - 1] and not s_cited[left_s - 1]
                   and mw[left_m - 1] == sw[left_s - 1]):
                check()
                left_m -= 1
                left_s -= 1
            while (right_m < len(mw) and right_s < len(sw) and mr[right_m] == mr[i] and sr[right_s] == sr[j]
                   and m_ok[right_m] and s_ok[right_s] and not m_cited[right_m] and not s_cited[right_s]
                   and mw[right_m] == sw[right_s]):
                check()
                right_m += 1
                right_s += 1
            initial = tuple(zip(range(left_m, right_m), range(left_s, right_s)))
            for x, y in initial[:-3]:
                visited_seeds.add((x, y))
            stack = [(initial, -1, frozenset())]
            retained_bytes = 256 * len(initial)
            while stack:
                check()
                if memory + retained_bytes + len(visited_seeds) * 128 > working_bytes:
                    raise limit("alignment-memory-limit", "Anchor-extension alternatives exceeded working memory.")
                path, direction, previous_reasons = stack.pop()
                retained_bytes -= 256 * len(path)
                a, b = path[0] if direction == -1 else path[-1]

                def neighbors(at, words, region, ok, cited, rank):
                    out = []
                    cursor = at + direction
                    citation_count = 0
                    stopped = "document-or-exclusion-boundary"
                    while 0 <= cursor < len(words) and region[cursor] == region[at] and ok[cursor]:
                        check()
                        if cited[cursor]:
                            citation_count += 1
                            if citation_count > PROFILE["max_citation_interruption_tokens"]:
                                stopped = "citation-interruption-budget"
                                break
                        else:
                            if abs(rank[cursor] - rank[at]) > PROFILE["max_gap_per_side"] + 1:
                                stopped = "maximum-gap-search-window"
                                break
                            out.append(cursor)
                        cursor += direction
                    return out, stopped

                next_m, m_stop = neighbors(a, mw, mr, m_ok, m_cited, m_rank)
                next_s, s_stop = neighbors(b, sw, sr, s_ok, s_cited, s_rank)
                choices = [(x, y) for x in next_m for y in next_s if mw[x] == sw[y]]
                # Only discard a bypass when inserting its earlier equal pair is
                # itself admissible; never discard a viable alternative for score.
                accepted, reasons = [], set()
                for pair in choices:
                    candidate = (pair,) + path if direction == -1 else path + (pair,)
                    info, reason = continuity(candidate, m_rank, s_rank, informative)
                    if reason:
                        reasons.add(reason)
                    else:
                        accepted.append((pair, candidate))
                if accepted:
                    nearest = [(pair, candidate) for pair, candidate in accepted if not any(
                        (other[0] - a) * direction < (pair[0] - a) * direction
                        and (other[1] - b) * direction < (pair[1] - b) * direction
                        for other, _ in accepted)]
                    for _, candidate in reversed(nearest):
                        retained_bytes += 256 * len(candidate)
                        stack.append((candidate, direction, previous_reasons))
                else:
                    side = "left" if direction == -1 else "right"
                    stopped = reasons or {"no-equal-prose-continuation", "manuscript-" + m_stop, "source-" + s_stop}
                    termination = previous_reasons | frozenset(f"{side}:{reason}" for reason in stopped)
                    if direction == -1:
                        retained_bytes += 256 * len(path)
                        stack.append((path, 1, termination))
                    elif len(path) >= PROFILE["minimum_qualifying_matches"]:
                        info, _ = continuity(path, m_rank, s_rank, informative)
                        info.update(
                            longest_exact_run=_longest(path), anchor_length=4,
                            anchor_pairs=[list(p) for p in seed],
                            anchor_strength={"content_words": 4, "uncommon_words": sum(w not in generic for w in seed_words),
                                             "commonness_basis": "document-pair frequency, not full-corpus document frequency"},
                            citation_token_contribution={"anchor": 0, "minimum_match": 0, "density": 0, "score": 0,
                                "manuscript_tokens_inside": sum(m_cited[path[0][0]:path[-1][0] + 1]),
                                "source_tokens_inside": sum(s_cited[path[0][1]:path[-1][1] + 1])},
                            reason_match_terminated=sorted(termination),
                        )
                        yield path, info
