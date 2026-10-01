"""Ordered alignment with opt-in improvedEng precision and original evidence."""
from __future__ import annotations

import hashlib
import math
import re
import time
from collections import defaultdict
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from typing import Callable, Iterator
from buna.citation_tokens import APA_VERSION, improved_citation_mask as citation_mask
from buna.span_evaluation import ledger_sha256
from buna.match_diagnostics import alignment_details, raw_citation_pairs
from buna.improved_layout import LAYOUT_VERSION, running_headers
from buna.improved_tokens import OPERATORS, WORD_POLICY_VERSION, tokens

from buna.classified import (
    _attribution, _exact_runs, _regions, _side,
)
from buna.comparison import (
    ComparisonCancelled, MAX_DOCUMENT_CHARACTERS, MAX_EVIDENCE_BYTES,
    MAX_SOURCE_SECONDS, _intervals, _line_join_words, _manuscript_view,
    _mask, _passage, _quotation_intervals,
)
from buna.documents import SOURCE_MAX_CHARACTERS, abstract_start
from buna.score_policy import (
    SCORE_BASIS, SCORE_POLICY_VERSION, manuscript_word_accounting,
)

MODEL_ID = "improvedEng"
VERSION = "improvedEng-v3.1-precision"
NORMALIZATION_VERSION = "nfkc-casefold-literal-numeric-document-local-hyphens-layout-operators-v3"
WORKING_INDEX_BYTES = 128 * 1024 * 1024
Pair = tuple[int, int]
Path = tuple[Pair, ...]
CONTENT_POLICY_VERSION = "literal-meaningful-types-numbers-statistics-v2"
_STOPWORDS = frozenset((
    "a an and are as at be been being but by can could did do does done for from had has have having he her "
    "hers him his how i if in into is it its itself may might more most must no nor not of off on once only or "
    "other our ours out over own same she should so some such than that the their theirs them then there these "
    "they this those through to too under until up very was we were what when where which while who whom why "
    "will with would you your also both each few further here just via per vs al et"
).split())
_STATISTICS = frozenset("b d f m n p r t z df sd se sem ci es η β χ μ σ ρ".split())
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
ELIGIBILITY_PROFILE = "improvedEng-layout-longquotes-operators-v2"
DENOMINATOR_DESCRIPTION = (
    "Unique original manuscript word units including literal =, <, > and approx-equal operators, excluding confirmed running headers, front matter before the recognized "
    "Abstract, bibliography, and balanced quotations longer than three words when enabled. Overlapping exclusions count "
    "once. Short quotations, citations and unmatched eligible words remain in the denominator. Source exclusions never "
    "remove manuscript denominator words. This is an application policy, not verified vendor arithmetic."
)


def _content(word: str) -> bool:
    return (word in _STATISTICS or _NUMBER.fullmatch(word) is not None
            or (len(word) >= 2 and word.isalpha() and word not in _STOPWORDS))


def content_details(path: Path, mw: list[str], m_cited: list[bool], s_cited: list[bool]) -> dict:
    distinct = set()
    run = []
    longest = strongest = 0
    previous = None
    for a, b in path:
        if m_cited[a] or s_cited[b]:
            run, previous = [], None
            continue
        if previous is None or (a, b) != (previous[0] + 1, previous[1] + 1):
            run = []
        content = _content(mw[a])
        if content:
            distinct.add(mw[a])
        run.append(content)
        longest = max(longest, len(run))
        if len(run) >= 3:
            strongest = max(strongest, sum(run[-3:]))
        previous = (a, b)
    return {"distinct_matched_content_words": len(distinct), "longest_exact_run": longest,
            "strongest_three_word_run_meaningful_words": strongest,
            "content_policy_version": CONTENT_POLICY_VERSION}


def accepts_similar(path: Path, mw: list[str], m_cited: list[bool], s_cited: list[bool]) -> bool:
    info = content_details(path, mw, m_cited, s_cited)
    return valid(path) and info["distinct_matched_content_words"] >= 4 and info["strongest_three_word_run_meaningful_words"] >= 1


@dataclass(frozen=True)
class Config:
    anchor_size: int = 3
    min_matched_words: int = 9
    max_gap: int = 5
    min_region_similarity: float = 0.60
    max_total_span: int | None = None

    def __post_init__(self):
        if (self.anchor_size != 3 or self.min_matched_words != 9 or self.max_gap != 5
                or self.min_region_similarity != 0.60 or self.max_total_span is not None):
            raise ValueError("improvedEng uses the fixed 3/9/5/0.60 retrieval profile with no span cap.")


class SearchLimit(Exception):
    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind = kind
        self.reason = reason


def measures(path: Path) -> dict:
    matched = len(path)
    m_span = path[-1][0] - path[0][0] + 1
    s_span = path[-1][1] - path[0][1] + 1
    m_gap = max((b[0] - a[0] - 1 for a, b in zip(path, path[1:])), default=0)
    s_gap = max((b[1] - a[1] - 1 for a, b in zip(path, path[1:])), default=0)
    return {
        "matched_words": matched, "manuscript_span": m_span, "source_span": s_span,
        "manuscript_similarity": matched / m_span, "source_similarity": matched / s_span,
        "manuscript_max_gap": m_gap, "source_max_gap": s_gap,
        "max_unmatched_run": max(m_gap, s_gap),
    }


def _seed_ends(path: Path) -> list[int]:
    ends, run = [], 0
    previous = None
    for index, pair in enumerate(path):
        run = run + 1 if previous and pair == (previous[0] + 1, previous[1] + 1) else 1
        if run >= 3:
            ends.append(index)
        previous = pair
    return ends


def valid(path: Path) -> bool:
    if len(path) < 9 or not _seed_ends(path):
        return False
    if any(b[0] <= a[0] or b[1] <= a[1] for a, b in zip(path, path[1:])):
        return False
    info = measures(path)
    return (info["max_unmatched_run"] <= 5
            and 5 * len(path) >= 3 * info["manuscript_span"]
            and 5 * len(path) >= 3 * info["source_span"])


def _windows(path: Path, check: Callable[[], None]) -> Iterator[Path]:
    """All inclusion-maximal valid windows of one gap-legal monotone path."""
    check()
    if len(path) < 9:
        return
    ends = _seed_ends(path)
    if not ends:
        return
    if valid(path):
        yield path
        return
    # A failed density prefix may recover; only containing valid windows dominate.
    covered_end, seed_cursor = -1, 0
    for start in range(len(path) - 8):
        check()
        while seed_cursor < len(ends) and ends[seed_cursor] < start + 2:
            seed_cursor += 1
        if seed_cursor == len(ends):
            break
        for end in range(len(path) - 1, max(start + 7, covered_end), -1):
            check()
            count = end - start + 1
            if (ends[seed_cursor] <= end
                    and 5 * count >= 3 * (path[end][0] - path[start][0] + 1)
                    and 5 * count >= 3 * (path[end][1] - path[start][1] + 1)):
                covered_end = end
                yield path[start:end + 1]
                break


def _neighbors(pair: Pair, mw: list[str], sw: list[str], mr: list[int], sr: list[int],
               m_ok: list[bool], s_ok: list[bool], m_cited: list[bool], s_cited: list[bool],
               direction: int) -> list[Pair]:
    i, j = pair
    result = []
    for di in range(1, 7):
        a = i + di * direction
        if not 0 <= a < len(mw) or mr[a] != mr[i] or not m_ok[a]:
            break
        if m_cited[a]:
            continue
        for dj in range(1, 7):
            b = j + dj * direction
            if not 0 <= b < len(sw) or sr[b] != sr[j] or not s_ok[b]:
                break
            if not s_cited[b] and mw[a] == sw[b]:
                result.append((a, b))
    return result


def _paths(graph: dict[Pair, tuple[Pair, ...]], check: Callable[[], None]) -> Iterator[Path]:
    incoming = {child for children in graph.values() for child in children}
    for root in sorted(graph.keys() - incoming):
        path = [root]
        stack = [iter(graph[root])]
        while stack:
            check()
            if not graph[path[-1]]:
                yield tuple(path)
            child = next(stack[-1], None)
            if child is None:
                stack.pop()
                path.pop()
            else:
                path.append(child)
                stack.append(iter(graph[child]))


def search(mw: list[str], sw: list[str], m_ok: list[bool], s_ok: list[bool],
           check: Callable[[], None], *, working_bytes: int = WORKING_INDEX_BYTES,
           m_cited: list[bool] | None = None, s_cited: list[bool] | None = None,
           m_skipped: list[bool] | None = None, s_skipped: list[bool] | None = None) -> Iterator[Path]:
    """Original v1 search; citation tokens cannot seed or qualify graph nodes."""
    m_cited = m_cited if m_cited is not None else [False] * len(mw)
    s_cited = s_cited if s_cited is not None else [False] * len(sw)
    if not (len(mw) == len(m_ok) == len(m_cited) and len(sw) == len(s_ok) == len(s_cited)):
        raise ValueError("Eligibility/citation masks must use the original word ledger.")
    if m_skipped is not None:
        if len(m_skipped) != len(mw):
            raise ValueError("Skipped quotation mask must use the logical word ledger.")
        m_cited = [c or q for c, q in zip(m_cited, m_skipped)]
    if s_skipped is not None:
        if len(s_skipped) != len(sw):
            raise ValueError("Skipped quotation mask must use the logical word ledger.")
        s_cited = [c or q for c, q in zip(s_cited, s_skipped)]
    mr, sr = _regions(m_ok), _regions(s_ok)
    table: dict[tuple[str, ...], list[int]] = defaultdict(list)
    index_bytes = 0
    for j in range(len(sw) - 2):
        check()
        if all(s_ok[j:j + 3]) and sr[j] == sr[j + 2] and not any(s_cited[j:j + 3]):
            index_bytes += 384
            if index_bytes > working_bytes:
                raise SearchLimit("index-memory-limit", "Trigram index reached the working-memory budget.")
            table[tuple(sw[j:j + 3])].append(j)
    seen: set[Pair] = set()
    for i in range(len(mw) - 2):
        check()
        if not all(m_ok[i:i + 3]) or mr[i] != mr[i + 2] or any(m_cited[i:i + 3]):
            continue
        for j in table.get(tuple(mw[i:i + 3]), ()):
            check()
            if (i, j) in seen:
                continue
            frontier = [(i, j)]
            nodes = {(i, j)}
            graph: dict[Pair, tuple[Pair, ...]] = {}
            while frontier:
                check()
                if index_bytes + 160 * len(seen) + 4096 * len(nodes) > working_bytes:
                    raise SearchLimit("alignment-memory-limit", "Ordered alignment graph reached the working-memory budget.")
                pair = frontier.pop()
                successors = _neighbors(pair, mw, sw, mr, sr, m_ok, s_ok, m_cited, s_cited, 1)
                # Remove only edges with an insertable equal pair between endpoints.
                graph[pair] = tuple(sorted(p for p in successors if not any(
                    q[0] < p[0] and q[1] < p[1] for q in successors)))
                for neighbor in successors + _neighbors(pair, mw, sw, mr, sr, m_ok, s_ok, m_cited, s_cited, -1):
                    if neighbor not in nodes:
                        nodes.add(neighbor)
                        frontier.append(neighbor)
            for path in _paths(graph, check):
                yield from _windows(path, check)
            seen.update(nodes)


def _contains(outer: Path, inner: Path) -> bool:
    if len(outer) < len(inner) or outer[0] > inner[0] or outer[-1] < inner[-1]:
        return False
    return set(inner).issubset(outer)


class _Evidence:
    def __init__(self, limit=None, label="Retained evidence"):
        self.bytes = 0
        self.peak_bytes = 0
        self.limit = MAX_EVIDENCE_BYTES if limit is None else limit
        self.label = label

    def add(self, size: int):
        if self.bytes + size > self.limit:
            raise SearchLimit("evidence-memory-limit", f"{self.label} reached its bounded memory budget; retained evidence is partial.")
        self.bytes += size
        self.peak_bytes = max(self.peak_bytes, self.bytes)

    def release(self, size: int):
        if not 0 <= size <= self.bytes:
            raise RuntimeError("Evidence memory accounting is inconsistent.")
        self.bytes -= size


def _path_bytes(path: Path) -> int:
    return 512 + len(path) * 192


def _is_exact(path: Path) -> bool:
    return len(path) == path[-1][0] - path[0][0] + 1 == path[-1][1] - path[0][1] + 1


def _envelope(path: Path):
    return path[0], path[-1], _is_exact(path)


def _collect(paths: list[Path], candidate: Path, budget: _Evidence, check: Callable[[], None],
             accept: Callable[[Path], bool] = valid) -> None:
    """Keep maximal pair-compatible evidence, never union unrelated envelopes."""
    check()
    for old in paths:
        check()
        if _contains(old, candidate) and (not _is_exact(candidate) or _is_exact(old)):
            return
    # A shared pair ties evidence to the same occurrence. Never fill an envelope
    # with invented pairs or join two independent repetitions of the wording.
    consumed = set()
    changed = True
    while changed:
        changed = False
        for index, old in enumerate(paths):
            check()
            if index in consumed or _is_exact(old) != _is_exact(candidate):
                continue
            if not (set(old) & set(candidate)):
                continue
            union = tuple(sorted(set(old) | set(candidate)))
            if (all(a[0] < b[0] and a[1] < b[1] for a, b in zip(union, union[1:]))
                    and (_is_exact(union) or accept(union))):
                candidate = union
                consumed.add(index)
                changed = True
    # Reserve before modifying retained evidence so interrupted work stays valid.
    budget.add(_path_bytes(candidate))
    kept = []
    for old in paths:
        if not _contains(candidate, old) or (_is_exact(old) and not _is_exact(candidate)):
            kept.append(old)
        else:
            budget.release(_path_bytes(old))
    paths[:] = kept
    paths.append(candidate)


def _long_quotes(text, ledger, headers=None):
    recognized, uncertain = _quotation_intervals(text)
    headers = headers if headers is not None else [False] * len(ledger)
    starts, ends = [t[1] for t in ledger], [t[2] for t in ledger]
    prefix = [0]
    for header in headers:
        prefix.append(prefix[-1] + int(not header))
    # Count the established ledger, including its hyphen joins, after removal
    # of layout artifacts. A nested short quote stays inside its long outer quote.
    long_intervals = [(a, b) for a, b in recognized
                      if prefix[bisect_left(starts, b)] - prefix[bisect_right(ends, a)] > 3]
    mask = _mask(ledger, long_intervals)
    return [q and not h for q, h in zip(mask, headers)], _mask(ledger, sorted(uncertain))


def improved_report(manuscript: dict, sources: list[dict], *, config: dict | None = None,
                    exclude_quotes: bool = True, manuscript_scope: str = "abstract-onward",
                    load_document: Callable[[dict], dict] | None = None,
                    cancelled: Callable[[], bool] | None = None,
                    source_seconds: float = MAX_SOURCE_SECONDS,
                    total_time_limit_seconds: float | None = None,
                    progress: Callable[[str], None] | None = None,
                    source_progress: Callable[[dict], None] | None = None) -> dict:
    cfg = Config(**(config or {}))
    if not math.isfinite(source_seconds) or not 0 < source_seconds <= MAX_SOURCE_SECONDS:
        raise ValueError("Source time limit must be positive and at most 120 seconds.")
    if total_time_limit_seconds is not None and not 0 < total_time_limit_seconds <= 600:
        raise ValueError("Total comparison time limit must be positive and at most 600 seconds.")
    if manuscript_scope not in {"abstract-onward", "whole-document"}:
        raise ValueError("Unsupported manuscript scope.")
    deadline = time.monotonic() + total_time_limit_seconds if total_time_limit_seconds is not None else None

    def check():
        if cancelled and cancelled():
            raise ComparisonCancelled("Comparison cancelled.")

    text = manuscript.get("text", "")
    if len(text) > MAX_DOCUMENT_CHARACTERS:
        raise ValueError("Manuscript exceeds the 1,000,000-character comparison limit.")
    # A stable manuscript ledger must not depend on source order or availability.
    mt = tokens(text, _line_join_words([text]), check)
    headers, layout_warnings = running_headers(manuscript, mt)
    m_map = [i for i in range(len(mt)) if not headers[i]]
    mw = [mt[i][0] for i in m_map]
    original_cited = citation_mask(text, mt, headers)
    m_cited = [original_cited[i] for i in m_map]
    bib = _mask(mt, _intervals(manuscript))
    quoted, uncertain = _long_quotes(text, mt, headers)
    front = [False] * len(mt)
    scope = {"requested": manuscript_scope, "applied": "whole-document", "start_offset": None,
             "start_page": None, "heading_text": None}
    warnings = list(manuscript.get("warnings", [])) + layout_warnings
    if manuscript_scope == "abstract-onward":
        found = abstract_start(manuscript)
        if found["start_offset"] is not None:
            scope.update(found, applied="abstract-onward")
            front = [t[1] < found["start_offset"] for t in mt]
        else:
            warnings.append("Abstract heading not detected; the whole manuscript was analyzed (front matter was not excluded).")
    eligible = [not (b or f or h or (exclude_quotes and q)) for b, f, h, q in zip(bib, front, headers, quoted)]
    m_ok = [eligible[i] for i in m_map]
    m_hard = [not (bib[i] or front[i]) for i in m_map]
    m_skipped = [exclude_quotes and quoted[i] for i in m_map]
    accounting = manuscript_word_accounting(eligible, front, bib, quoted, exclude_quotes=exclude_quotes)
    denominator = accounting["score_denominator_words"]
    numbers = {str(s.get("id", f"source-{i + 1}")): s.get("source_number", i + 1) for i, s in enumerate(sources)}
    ordered = sorted(enumerate(sources), key=lambda item: str(item[1].get("id", f"source-{item[0] + 1}")))
    coverage, matches, raw = [], [], []
    audit_limit = MAX_EVIDENCE_BYTES // 8
    evidence_budget = _Evidence(MAX_EVIDENCE_BYTES - audit_limit, "Scored comparison evidence")
    audit_budget = _Evidence(audit_limit, "Optional excluded-text audit evidence")
    audit_memory_exhausted = False
    exact_words: dict[str, set[int]] = defaultdict(set)
    similar_words: dict[str, set[int]] = defaultdict(set)

    for number, (original_index, source) in enumerate(ordered):
        check()
        sid = str(source.get("id", f"source-{original_index + 1}"))
        row = {"source_id": sid, "status": "compared", "limits_reached": [],
               "scored_search_complete": False, "audit_search_complete": False,
               "preprint_status": "unknown", "exact_matches": 0, "similar_matches": 0,
               "similar_qualification_rejections": {"distinct-content": 0, "three-word-anchor": 0}}
        coverage.append(row)
        started = time.monotonic()
        remaining_sources = sum(not item.get("excluded") for _, item in ordered[number:])
        fair_seconds = min(source_seconds, max(0, (deadline - started) / max(1, remaining_sources))) if deadline else source_seconds
        row["allocated_source_seconds"] = fair_seconds
        audit_deadline = None

        def budget():
            check()
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise SearchLimit("total-time-limit", "Total comparison time ended; retained evidence is partial.")
            if audit_deadline is not None and now >= audit_deadline:
                raise SearchLimit("audit-time-limit", "Excluded-text audit slice ended; scored search is unchanged.")
            if now - started >= fair_seconds:
                raise SearchLimit("source-time-limit", "Fair per-source comparison time ended; retained evidence is partial.")

        def done():
            if source_progress:
                source_progress(row)

        if source.get("excluded"):
            row.update(status="excluded-by-user", reason=source.get("exclusion_reason") or "Explicit source exclusion.")
            done()
            continue
        if deadline is not None and time.monotonic() >= deadline:
            row.update(status="skipped-time-limit", reason="Total comparison time ended before this source.",
                       limits_reached=["total-time-limit"])
            done()
            continue
        try:
            document = load_document(source) if load_document else source.get("document")
        except (OSError, ValueError) as exc:
            row.update(status="unavailable", reason=str(exc))
            done()
            continue
        if not document or not document.get("text"):
            row.update(status="unavailable", reason="No readable extracted text.")
            done()
            continue
        if len(document["text"]) > SOURCE_MAX_CHARACTERS:
            row.update(status="skipped-size-limit", reason="Source exceeds the 2,000,000-character comparison limit.")
            done()
            continue
        scored_paths: list[Path] = []
        audit_paths: list[Path] = []
        st = []
        phase = "scored"
        try:
            budget()
            st = tokens(document["text"], _line_join_words([document["text"]]), budget)
            s_headers, s_warnings = running_headers(document, st)
            warnings.extend(f"Source {sid}: {warning}" for warning in s_warnings)
            s_map = [i for i in range(len(st)) if not s_headers[i]]
            sw = [st[i][0] for i in s_map]
            row.update(source_total_words=len(st), header_removed_words=sum(s_headers))
            source_cited = citation_mask(document["text"], st, s_headers)
            s_cited = [source_cited[i] for i in s_map]
            row["citation_recognized_words"] = sum(s_cited)
            row["source_content_sha256"] = hashlib.sha256(document["text"].encode()).hexdigest()
            s_bib = _mask(st, _intervals(document))
            s_ok = [not s_bib[i] for i in s_map]
            s_quoted, _ = _long_quotes(document["text"], st, s_headers)
            s_skipped = [exclude_quotes and s_quoted[i] for i in s_map]
            s_exact = [ok and not q for ok, q in zip(s_ok, s_skipped)]
            if sw == mw:
                row.update(status="excluded-identical", reason="Identical normalized manuscript/source text.")
                done()
                continue
            if progress:
                progress(f"Comparing source {number + 1}/{len(sources)}: improvedEng ordered wording.")
            # Publish cheap real exact evidence even if the harder graph later stops.
            from buna.classified import _Limit as ExactLimit
            try:
                for a, e, b, f in _exact_runs(mw, m_ok, _regions(m_ok), sw, s_exact, _regions(s_exact), 9, budget):
                    _collect(scored_paths, tuple(zip(range(a, e), range(b, f))), evidence_budget, budget)
            except ExactLimit as exc:
                raise SearchLimit(exc.kind, exc.reason) from exc
            for path in search(mw, sw, m_hard, s_ok, budget, m_cited=m_cited, s_cited=s_cited,
                               m_skipped=m_skipped, s_skipped=s_skipped,
                               working_bytes=WORKING_INDEX_BYTES):
                if accepts_similar(path, mw, m_cited, s_cited):
                    _collect(scored_paths, path, evidence_budget, budget,
                             lambda p: accepts_similar(p, mw, m_cited, s_cited))
                else:
                    rejected = content_details(path, mw, m_cited, s_cited)
                    stage = ("distinct-content" if rejected["distinct_matched_content_words"] < 4
                             else "three-word-anchor")
                    row["similar_qualification_rejections"][stage] += 1
            row["scored_search_complete"] = True
            phase = "audit"
            # Audit work must not consume the rest of the budget for later sources.
            audit_deadline = min(started + fair_seconds, time.monotonic() + fair_seconds * 0.1)
            if not all(m_ok) or not all(s_exact):
                if audit_memory_exhausted:
                    raise SearchLimit("audit-memory-limit", "Optional excluded-text audit memory was exhausted; scored search is complete and unchanged.")
                for path in search(mw, sw, [True] * len(mw), [True] * len(sw), budget,
                                   m_cited=m_cited, s_cited=s_cited, working_bytes=WORKING_INDEX_BYTES):
                    ma, mb, sa, sb = path[0][0], path[-1][0] + 1, path[0][1], path[-1][1] + 1
                    if (any(not m_ok[a] or not s_exact[b] for a, b in path)
                            and accepts_similar(path, mw, m_cited, s_cited)):
                        _collect(audit_paths, path, audit_budget, budget,
                                 lambda p: accepts_similar(p, mw, m_cited, s_cited))
                try:
                    for a, e, b, f in _exact_runs(mw, [True] * len(mw), [0] * len(mw), sw, [True] * len(sw),
                                                [0] * len(sw), 9, budget):
                        path = tuple(zip(range(a, e), range(b, f)))
                        if not all(m_ok[a:e]) or not all(s_exact[b:f]):
                            _collect(audit_paths, path, audit_budget, budget)
                except ExactLimit as exc:
                    raise SearchLimit(exc.kind, exc.reason) from exc
            row["audit_search_complete"] = True
        except SearchLimit as exc:
            if phase == "audit" and exc.kind in {"evidence-memory-limit", "audit-memory-limit"}:
                audit_memory_exhausted = True
            row["audit_limit_reason" if phase == "audit" else "scored_limit_reason"] = exc.reason
            row.update(reason=exc.reason, incomplete_stage=phase)
            row["limits_reached"].append(exc.kind)
            if not row["scored_search_complete"]:
                row["status"] = "compared-with-limits"
            else:
                warnings.append(f"Source {sid}: excluded-text audit incomplete ({exc.kind}); scored search completed.")

        for excluded, paths in ((False, scored_paths), (True, audit_paths)):
            retained_budget = audit_budget if excluded else evidence_budget
            paths.sort(key=lambda p: (_envelope(p), p), reverse=True)
            while paths:
                path = paths.pop()
                check()
                alternatives = [path]
                while paths and _envelope(paths[-1]) == _envelope(path):
                    alternatives.append(paths.pop())
                # One span record, with compact pair provenance for incompatible
                # alternatives. Every alternative passed acceptance independently.
                alternatives.sort(key=lambda p: (-len(p), p))
                path = alternatives[0]
                original_path = tuple((m_map[a], s_map[b]) for a, b in path)
                m_positions = sorted({m_map[a] for p in alternatives for a, _ in p})
                s_positions = sorted({s_map[b] for p in alternatives for _, b in p})
                ma, mb, sa, sb = m_positions[0], m_positions[-1] + 1, s_positions[0], s_positions[-1] + 1
                info = measures(path)
                kind = "exact" if info["matched_words"] == info["manuscript_span"] == info["source_span"] else "similar"
                # Reserve strings and offset arrays before materializing the report.
                try:
                    retained_budget.add(10240 + 768 * sum(map(len, alternatives)) + 8 * (
                        mt[mb - 1][2] - mt[ma][1] + st[sb - 1][2] - st[sa][1]))
                except SearchLimit as exc:
                    row["audit_limit_reason" if excluded else "scored_limit_reason"] = exc.reason
                    if not excluded:
                        row.update(status="compared-with-limits", scored_search_complete=False)
                    else:
                        row["audit_search_complete"] = False
                        audit_memory_exhausted = True
                        warnings.append(f"Source {sid}: optional excluded-text evidence incomplete; scored evidence is unchanged.")
                    row.update(reason=exc.reason, incomplete_stage="evidence")
                    if exc.kind not in row["limits_reached"]:
                        row["limits_reached"].append(exc.kind)
                    for disposed in alternatives:
                        retained_budget.release(_path_bytes(disposed))
                    for pending_path in paths:
                        retained_budget.release(_path_bytes(pending_path))
                    paths.clear()
                    break
                passage, attribution = _attribution(manuscript, mt, source, m_positions, quoted, uncertain)
                m_side = _side(manuscript, mt, m_positions, ma, mb - 1)
                s_side = _side(document, st, s_positions, sa, sb - 1)
                reasons = []
                if any(front[ma:mb]):
                    reasons.append("front-matter")
                if any(bib[ma:mb]):
                    reasons.append("bibliography")
                if exclude_quotes and any(quoted[i] for i in m_positions):
                    reasons.append("recognized-quotation")
                if any(s_bib[sa:sb]):
                    reasons.append("source-bibliography")
                if exclude_quotes and any(s_quoted[i] for i in s_positions):
                    reasons.append("source-quotation")
                match = {
                    "source_id": sid, "source_number": numbers[sid], "source_content_sha256": row["source_content_sha256"],
                    "kind": "exact" if kind == "exact" else "near-verbatim", "match_kind": kind,
                    "manuscript": {**passage, "word_start": ma, "word_end": mb,
                                   "equal_spans": m_side["equal_spans"]},
                    "source": {**s_side, "match_start": s_side["start"], "match_end": s_side["end"],
                               "highlights": [[a - s_side["start"], b - s_side["start"]] for a, b in s_side["equal_spans"]]},
                    "aligned_pairs": [list(p) for p in original_path], **info,
                    "alternative_alignments": [
                        [[m_map[a], s_map[b]] for a, b in alternative] for alternative in alternatives[1:]],
                    "similarity": min(info["manuscript_similarity"], info["source_similarity"]),
                    "scored_word_positions": [] if excluded else m_positions,
                    "included_words": 0 if excluded else len(m_positions), "excluded_words": len(m_positions) if excluded else 0,
                    "excluded_from_score": excluded, "exclusion_reasons": reasons, **attribution,
                    "edits": {"manuscript_unmatched": info["manuscript_span"] - len(path),
                              "source_unmatched": info["source_span"] - len(path)},
                    "order": {"out_of_order_blocks": 0},
                }
                raw_pairs = raw_citation_pairs(path, mw, sw, m_cited, s_cited) if kind == "similar" else path
                match["raw_aligned_pairs"] = [[m_map[a], s_map[b]] for a, b in raw_pairs]
                match["qualifying_aligned_pairs"] = [list(pair) for pair in original_path]
                details = alignment_details(raw_pairs, mw, sw, m_cited, s_cited)
                details.update(content_details(path, mw, m_cited, s_cited))
                details.update(manuscript_span=[ma, mb], source_span=[sa, sb],
                               logical_manuscript_span=[path[0][0], path[-1][0] + 1],
                               logical_source_span=[path[0][1], path[-1][1] + 1],
                               header_removed_words=sum(headers[ma:mb]),
                               source_header_removed_words=sum(s_headers[sa:sb]),
                               matched_quoted_words=sum(quoted[i] for i in m_positions),
                               source_matched_quoted_words=sum(s_quoted[i] for i in s_positions),
                               quotation_tokens_inside_span=sum(quoted[ma:mb]),
                               source_quotation_tokens_inside_span=sum(s_quoted[sa:sb]),
                               local_eligible_words=sum(eligible[ma:mb]))
                if details["seed"]:
                    details["seed"]["aligned_pairs"] = [[m_map[a], s_map[b]] for a, b in details["seed"]["aligned_pairs"]]
                details["citation_matches"]["aligned_pairs"] = [
                    [m_map[a], s_map[b]] for a, b in details["citation_matches"]["aligned_pairs"]]
                match["diagnostics"] = {
                    **details,
                    "matched_word_count": len(path), "raw_matched_word_count": len(raw_pairs),
                    "alignment_alternative_count": len(alternatives), "unique_target_matched_words": len(m_positions),
                    "source": sid, "source_content_sha256": row["source_content_sha256"],
                    "match_kind": kind, "exact_citation_policy": "unchanged; citation qualification correction applies to Similar",
                    "scored": not excluded,
                }
                (raw if excluded else matches).append(match)
                for disposed in alternatives:
                    retained_budget.release(_path_bytes(disposed))
                if not excluded:
                    (exact_words if kind == "exact" else similar_words)[sid].update(m_positions)
                    row[kind + "_matches"] += 1
        done()
        del document

    all_exact = set().union(*exact_words.values()) if exact_words else set()
    all_similar = (set().union(*similar_words.values()) if similar_words else set()) - all_exact
    covered = all_exact | all_similar
    complete = any(r["status"] == "compared" for r in coverage) and all(
        r["status"] in {"compared", "excluded-by-user", "excluded-identical"} for r in coverage)
    pct = lambda count: round(100 * count / denominator, 2) if denominator else None
    per_word: dict[int, set[str]] = defaultdict(set)
    for sid in exact_words.keys() | similar_words.keys():
        for index in exact_words[sid] | similar_words[sid]:
            per_word[index].add(sid)
    intervals = []
    unmatched, unchecked = 0, 0
    for i, token in enumerate(mt):
        check()
        state = ("excluded-front-matter" if front[i] else "excluded-bibliography" if bib[i]
                 else "excluded-running-header" if headers[i]
                 else "excluded-quotation" if exclude_quotes and quoted[i] else "exact" if i in all_exact
                 else "similar" if i in all_similar else "unmatched" if complete else "not-fully-checked")
        unmatched += int(state == "unmatched")
        unchecked += int(state == "not-fully-checked")
        ids = sorted(per_word.get(i, ()))
        if intervals and intervals[-1]["state"] == state and intervals[-1]["sources"] == ids:
            intervals[-1].update(end=token[2], word_end=i + 1)
        else:
            intervals.append({"state": state, "sources": ids, "start": token[1], "end": token[2],
                              "word_start": i, "word_end": i + 1})
    for match in matches:
        match["alternative_source_ids"] = sorted(set().union(
            *(per_word[i] for i in match["scored_word_positions"])) - {match["source_id"]})
    for row in coverage:
        if row["status"] in {"compared", "compared-with-limits"}:
            sid = row["source_id"]
            n_exact, n_similar = len(exact_words[sid]), len(similar_words[sid] - exact_words[sid])
            row.update(overlapping_words=n_exact + n_similar, exact_words=n_exact, similar_only_words=n_similar,
                       score_denominator_words=denominator, eligible_words=denominator,
                       exact_percent=pct(n_exact), similar_only_percent=pct(n_similar), overlap_percent=pct(n_exact + n_similar))
    metrics = {
        **accounting, "eligibility_profile": ELIGIBILITY_PROFILE,
        "operator_words": sum(token[0] in OPERATORS for token in mt),
        "header_removed_words": sum(headers), "citation_recognized_words": sum(m_cited),
        "overlapping_words": len(covered), "overlap_percent": pct(len(covered)),
        "exact_words": len(all_exact), "similar_only_words": len(all_similar),
        "combined_words": len(covered), "combined_percent": pct(len(covered)),
        "exact_percent": pct(len(all_exact)), "similar_only_percent": pct(len(all_similar)),
        "unmatched_words": unmatched, "not_fully_checked_words": unchecked,
        "sources_supplied": len(sources), "sources_compared": sum(
            r["status"] in {"compared", "compared-with-limits"} for r in coverage),
        "identical_sources_excluded": sum(r["status"] == "excluded-identical" for r in coverage),
        "all_sources_fully_checked": complete, "truncated": not complete,
        "match_records": len(matches),
        "distinct_target_match_spans": len({(m["manuscript"]["word_start"], m["manuscript"]["word_end"]) for m in matches}),
        "alignment_alternatives": sum(1 + len(m["alternative_alignments"]) for m in matches),
    }
    warnings += [
        "improvedEng is experimental ordered lexical matching, not semantic matching or a verified Crossref model.",
        "Preprint status is unknown unless explicitly supplied; no preprint classification is inferred.",
    ]
    if not complete:
        warnings.append("Not all sources were fully checked. Unmarked eligible text is not a finding of originality.")
    if not denominator:
        warnings.append("No eligible manuscript words remain; the comparison is unscorable.")
    matches.sort(key=lambda m: (m["manuscript"]["match_start"], m["source_id"], m["source"]["match_start"], m["aligned_pairs"]))
    settings = {"minimum_matched_words": 9, "exclude_bibliography": True, "exclude_quotes": exclude_quotes,
                "score_basis": SCORE_BASIS, "score_policy_version": SCORE_POLICY_VERSION,
                "eligibility_profile": ELIGIBILITY_PROFILE,
                "word_policy_version": WORD_POLICY_VERSION,
                "operator_policy": "Each literal =, <, > and \u2248 is one word unit for alignment/count/density/score; not meaningful content. Compound ASCII operators are separate literal units; no additional mathematical equivalences.",
                "manuscript_scope": scope, "improved_eng_config": asdict(cfg),
                "citation_qualification": {
                    "recognizer_version": APA_VERSION,
                    "scope": "Similar only; Exact unchanged",
                    "seed": "Three consecutive equal noncitation eligible words at header-cleaned positions on both sides",
                    "minimum_and_density_numerator": "Noncitation equal pairs only",
                    "gap_and_span_accounting": "Header-cleaned positions; citations and long quotes retain intervening gap/span positions",
                },
                "normalization_version": NORMALIZATION_VERSION, "source_time_limit_seconds": source_seconds,
                "layout_version": LAYOUT_VERSION,
                "similar_content_policy": {"version": CONTENT_POLICY_VERSION, "minimum_distinct_content_words": 4,
                                          "minimum_exact_run": 3, "minimum_meaningful_words_in_three_word_run": 1,
                                          "meaningful_words": "Non-stopword lexical types plus literal numbers and statistical tokens; citations never qualify"},
                "quotation_policy": "improvedEng: balanced quotes of at most three words ignored; longer quotes excluded individually on both sides when enabled, retaining gap/span positions",
                "evidence_deduplication": "One source/target-span/source-span record; compatible shared-pair unions revalidated, incompatible same-span alignments retained as compact alternative_alignments; Exact subruns preserved",
                "evidence_memory_policy": "live-retained-scored-priority-v1",
                "total_time_limit_seconds": total_time_limit_seconds, "working_index_limit_mib": 128}
    return {
        "algorithm_version": VERSION, "comparison_model": MODEL_ID, "settings": settings, "metrics": metrics,
        "quality_notice": "Experimental ordered lexical evidence for human review; vendor equivalence is not established.",
        "matches": matches, "source_coverage": coverage, "warnings": warnings,
        "excluded_source_ids": [r["source_id"] for r in coverage if r["status"] == "excluded-identical"],
        "manuscript_pages": _manuscript_view(
            manuscript, mt, [(m, set(m["scored_word_positions"])) for m in matches], covered, numbers),
        "classification": {"engine_candidate": VERSION, "normalization_version": NORMALIZATION_VERSION,
                           "metrics": metrics, "coverage_intervals": intervals,
                           "labels": {"similar": "Ordered equal wording with gaps; no reordering or semantic matching."}},
        "improved_eng": {"config": asdict(cfg), "raw_excluded_evidence": raw,
                         "evidence_memory": {
                             "combined_limit_bytes": MAX_EVIDENCE_BYTES,
                             "scored": {"limit_bytes": evidence_budget.limit, "retained_bytes": evidence_budget.bytes,
                                        "peak_bytes": evidence_budget.peak_bytes},
                             "excluded_audit": {"limit_bytes": audit_budget.limit, "retained_bytes": audit_budget.bytes,
                                                "peak_bytes": audit_budget.peak_bytes, "exhausted": audit_memory_exhausted},
                             "policy": "Scored evidence has a protected seven-eighths share; optional excluded audit has one eighth. Replaced and materialized temporary paths release their reservations.",
                         },
                         "manuscript_ledger_sha256": ledger_sha256(mt),
                         "similar_diagnostics": [m["diagnostics"] for m in matches + raw if m["match_kind"] == "similar"],
                         "alignment_diagnostics": [m["diagnostics"] for m in matches + raw],
                         "calibration_ready": bool(coverage) and denominator > 0 and all(r["status"] == "compared" for r in coverage),
                         "audit_complete": bool(coverage) and all(
                             r["audit_search_complete"] or r["status"] in {"excluded-by-user", "excluded-identical"}
                             for r in coverage),
                         "search": "Three-word-seeded ordered alternatives; four distinct meaningful words (including literal numbers/statistics) and a contiguous three-word run with one meaningful word required for Similar only. Headers removed logically; citations and long quotes retain gap positions."},
        "methodology": {
            "name": MODEL_ID, "version": VERSION, "normalization": NORMALIZATION_VERSION,
            "exact": "At least nine contiguous equal eligible normalized words after header removal; contiguous citations retain Exact credit; short quotes remain eligible.",
            "similar": "At least nine eligible noncitation equal words, four distinct meaningful types (including literal numbers/statistics), and a three-word contiguous exact run containing at least one meaningful word. Retrieval seed three, gap at most five and global density at least 60% independently per side. Citations and enabled long quotes consume gap/span positions.",
            "denominator": DENOMINATOR_DESCRIPTION,
            "limitations": "Lexical hypotheses, not vendor parameters. No seed means no candidate. Literal numbers; no citation deletion. Ambiguous headers retained with warnings. Resource-limited search explicitly partial.",
        },
    }
