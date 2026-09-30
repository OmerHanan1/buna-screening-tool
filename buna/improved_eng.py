"""Restored v1 ordered alignment with citation-only qualification correction."""
from __future__ import annotations

import hashlib
import math
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Callable, Iterator
from buna.citation_tokens import citation_mask
from buna.span_evaluation import ledger_sha256
from buna.match_diagnostics import alignment_details, raw_citation_pairs

from buna.classified import (
    _attribution, _exact_runs, _regions, _side, tokens,
)
from buna.comparison import (
    ComparisonCancelled, MAX_DOCUMENT_CHARACTERS, MAX_EVIDENCE_BYTES,
    MAX_SOURCE_SECONDS, _intervals, _line_join_words, _manuscript_view,
    _mask, _passage, _quotation_intervals,
)
from buna.documents import SOURCE_MAX_CHARACTERS, abstract_start
from buna.score_policy import (
    DENOMINATOR_DESCRIPTION, SCORE_BASIS, SCORE_POLICY_VERSION, manuscript_word_accounting,
)

MODEL_ID = "improvedEng"
VERSION = "improvedEng-v1-citation"
NORMALIZATION_VERSION = "nfkc-casefold-literal-numeric-document-local-hyphens-v1"
WORKING_INDEX_BYTES = 128 * 1024 * 1024
Pair = tuple[int, int]
Path = tuple[Pair, ...]


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
            raise ValueError("Restored improvedEng uses the fixed 3/9/5/0.60 profile with no span cap.")


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
           m_cited: list[bool] | None = None, s_cited: list[bool] | None = None) -> Iterator[Path]:
    """Original v1 search; citation tokens cannot seed or qualify graph nodes."""
    m_cited = m_cited if m_cited is not None else [False] * len(mw)
    s_cited = s_cited if s_cited is not None else [False] * len(sw)
    if not (len(mw) == len(m_ok) == len(m_cited) and len(sw) == len(s_ok) == len(s_cited)):
        raise ValueError("Eligibility/citation masks must use the original word ledger.")
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
    def __init__(self):
        self.bytes = 0

    def add(self, size: int):
        if self.bytes + size > MAX_EVIDENCE_BYTES:
            raise SearchLimit("evidence-memory-limit", "Retained evidence reached its memory budget; findings are partial.")
        self.bytes += size


def _collect(paths: list[Path], candidate: Path, budget: _Evidence, check: Callable[[], None]) -> None:
    """Keep maximal pair-compatible evidence, never union unrelated envelopes."""
    check()
    for old in paths:
        check()
        if _contains(old, candidate):
            return
    # Reserve before modifying retained evidence so interrupted work stays valid.
    budget.add(512 + len(candidate) * 192)
    def exact(path):
        return len(path) == path[-1][0] - path[0][0] + 1 == path[-1][1] - path[0][1] + 1

    paths[:] = [old for old in paths if not _contains(candidate, old) or (exact(old) and not exact(candidate))]
    paths.append(candidate)


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
    mw = [t[0] for t in mt]
    m_cited = citation_mask(text, mt)
    bib = _mask(mt, _intervals(manuscript))
    quote_intervals, uncertain_intervals = _quotation_intervals(text)
    quoted, uncertain = _mask(mt, quote_intervals), _mask(mt, sorted(uncertain_intervals))
    front = [False] * len(mt)
    scope = {"requested": manuscript_scope, "applied": "whole-document", "start_offset": None,
             "start_page": None, "heading_text": None}
    warnings = list(manuscript.get("warnings", []))
    if manuscript_scope == "abstract-onward":
        found = abstract_start(manuscript)
        if found["start_offset"] is not None:
            scope.update(found, applied="abstract-onward")
            front = [t[1] < found["start_offset"] for t in mt]
        else:
            warnings.append("Abstract heading not detected; the whole manuscript was analyzed (front matter was not excluded).")
    eligible = [not (b or f or (exclude_quotes and q)) for b, f, q in zip(bib, front, quoted)]
    accounting = manuscript_word_accounting(eligible, front, bib, quoted, exclude_quotes=exclude_quotes)
    denominator = accounting["score_denominator_words"]
    numbers = {str(s.get("id", f"source-{i + 1}")): s.get("source_number", i + 1) for i, s in enumerate(sources)}
    ordered = sorted(enumerate(sources), key=lambda item: str(item[1].get("id", f"source-{item[0] + 1}")))
    coverage, matches, raw = [], [], []
    evidence_budget = _Evidence()
    exact_words: dict[str, set[int]] = defaultdict(set)
    similar_words: dict[str, set[int]] = defaultdict(set)

    for number, (original_index, source) in enumerate(ordered):
        check()
        sid = str(source.get("id", f"source-{original_index + 1}"))
        row = {"source_id": sid, "status": "compared", "limits_reached": [],
               "scored_search_complete": False, "audit_search_complete": False,
               "preprint_status": "unknown", "exact_matches": 0, "similar_matches": 0}
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
            sw = [t[0] for t in st]
            row["source_total_words"] = len(sw)
            s_cited = citation_mask(document["text"], st)
            row["source_content_sha256"] = hashlib.sha256(document["text"].encode()).hexdigest()
            s_ok = [not b for b in _mask(st, _intervals(document))]
            if sw == mw:
                row.update(status="excluded-identical", reason="Identical normalized manuscript/source text.")
                done()
                continue
            if progress:
                progress(f"Comparing source {number + 1}/{len(sources)}: improvedEng ordered wording.")
            # Publish cheap real exact evidence even if the harder graph later stops.
            from buna.classified import _Limit as ExactLimit
            try:
                for a, e, b, f in _exact_runs(mw, eligible, _regions(eligible), sw, s_ok, _regions(s_ok), 9, budget):
                    _collect(scored_paths, tuple(zip(range(a, e), range(b, f))), evidence_budget, budget)
            except ExactLimit as exc:
                raise SearchLimit(exc.kind, exc.reason) from exc
            for path in search(mw, sw, eligible, s_ok, budget, m_cited=m_cited, s_cited=s_cited,
                               working_bytes=WORKING_INDEX_BYTES):
                _collect(scored_paths, path, evidence_budget, budget)
            row["scored_search_complete"] = True
            phase = "audit"
            # Audit work must not consume the rest of the budget for later sources.
            audit_deadline = min(started + fair_seconds, time.monotonic() + fair_seconds * 0.1)
            if not all(eligible) or not all(s_ok):
                for path in search(mw, sw, [True] * len(mw), [True] * len(sw), budget,
                                   m_cited=m_cited, s_cited=s_cited, working_bytes=WORKING_INDEX_BYTES):
                    ma, mb, sa, sb = path[0][0], path[-1][0] + 1, path[0][1], path[-1][1] + 1
                    if not all(eligible[ma:mb]) or not all(s_ok[sa:sb]):
                        _collect(audit_paths, path, evidence_budget, budget)
                try:
                    for a, e, b, f in _exact_runs(mw, [True] * len(mw), [0] * len(mw), sw, [True] * len(sw),
                                                [0] * len(sw), 9, budget):
                        path = tuple(zip(range(a, e), range(b, f)))
                        if not all(eligible[a:e]) or not all(s_ok[b:f]):
                            _collect(audit_paths, path, evidence_budget, budget)
                except ExactLimit as exc:
                    raise SearchLimit(exc.kind, exc.reason) from exc
            row["audit_search_complete"] = True
        except SearchLimit as exc:
            row.update(reason=exc.reason, incomplete_stage=phase)
            row["limits_reached"].append(exc.kind)
            if not row["scored_search_complete"]:
                row["status"] = "compared-with-limits"
            else:
                warnings.append(f"Source {sid}: excluded-text audit incomplete ({exc.kind}); scored search completed.")

        for excluded, paths in ((False, scored_paths), (True, audit_paths)):
            for path in sorted(paths):
                check()
                m_positions, s_positions = [p[0] for p in path], [p[1] for p in path]
                ma, mb, sa, sb = path[0][0], path[-1][0] + 1, path[0][1], path[-1][1] + 1
                info = measures(path)
                kind = "exact" if info["matched_words"] == info["manuscript_span"] == info["source_span"] else "similar"
                # Reserve strings and offset arrays before materializing the report.
                try:
                    evidence_budget.add(10240 + 768 * len(path) + 8 * (
                        mt[mb - 1][2] - mt[ma][1] + st[sb - 1][2] - st[sa][1]))
                except SearchLimit as exc:
                    if not excluded:
                        row.update(status="compared-with-limits", scored_search_complete=False)
                    else:
                        row["audit_search_complete"] = False
                    row.update(reason=exc.reason, incomplete_stage="evidence")
                    if exc.kind not in row["limits_reached"]:
                        row["limits_reached"].append(exc.kind)
                    break
                passage, attribution = _attribution(manuscript, mt, source, m_positions, quoted, uncertain)
                m_side = _side(manuscript, mt, m_positions, ma, mb - 1)
                s_side = _side(document, st, s_positions, sa, sb - 1)
                reasons = []
                if any(front[ma:mb]):
                    reasons.append("front-matter")
                if any(bib[ma:mb]):
                    reasons.append("bibliography")
                if exclude_quotes and any(quoted[ma:mb]):
                    reasons.append("recognized-quotation")
                if not all(s_ok[sa:sb]):
                    reasons.append("source-bibliography")
                match = {
                    "source_id": sid, "source_number": numbers[sid], "source_content_sha256": row["source_content_sha256"],
                    "kind": "exact" if kind == "exact" else "near-verbatim", "match_kind": kind,
                    "manuscript": {**passage, "word_start": ma, "word_end": mb,
                                   "equal_spans": m_side["equal_spans"]},
                    "source": {**s_side, "match_start": s_side["start"], "match_end": s_side["end"],
                               "highlights": [[a - s_side["start"], b - s_side["start"]] for a, b in s_side["equal_spans"]]},
                    "aligned_pairs": [list(p) for p in path], **info,
                    "similarity": min(info["manuscript_similarity"], info["source_similarity"]),
                    "scored_word_positions": [] if excluded else m_positions,
                    "included_words": 0 if excluded else len(path), "excluded_words": len(path) if excluded else 0,
                    "excluded_from_score": excluded, "exclusion_reasons": reasons, **attribution,
                    "edits": {"manuscript_unmatched": info["manuscript_span"] - len(path),
                              "source_unmatched": info["source_span"] - len(path)},
                    "order": {"out_of_order_blocks": 0},
                }
                raw_pairs = raw_citation_pairs(path, mw, sw, m_cited, s_cited) if kind == "similar" else path
                match["raw_aligned_pairs"] = [list(pair) for pair in raw_pairs]
                match["qualifying_aligned_pairs"] = [list(pair) for pair in path]
                match["diagnostics"] = {
                    **alignment_details(raw_pairs, mw, sw, m_cited, s_cited),
                    "matched_word_count": len(path), "raw_matched_word_count": len(raw_pairs),
                    "source": sid, "source_content_sha256": row["source_content_sha256"],
                    "match_kind": kind, "exact_citation_policy": "unchanged; citation qualification correction applies to Similar",
                    "scored": not excluded,
                }
                (raw if excluded else matches).append(match)
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
        **accounting, "overlapping_words": len(covered), "overlap_percent": pct(len(covered)),
        "exact_words": len(all_exact), "similar_only_words": len(all_similar),
        "combined_words": len(covered), "combined_percent": pct(len(covered)),
        "exact_percent": pct(len(all_exact)), "similar_only_percent": pct(len(all_similar)),
        "unmatched_words": unmatched, "not_fully_checked_words": unchecked,
        "sources_supplied": len(sources), "sources_compared": sum(
            r["status"] in {"compared", "compared-with-limits"} for r in coverage),
        "identical_sources_excluded": sum(r["status"] == "excluded-identical" for r in coverage),
        "all_sources_fully_checked": complete, "truncated": not complete,
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
                "manuscript_scope": scope, "improved_eng_config": asdict(cfg),
                "citation_qualification": {
                    "scope": "Similar only; Exact unchanged",
                    "seed": "Three consecutive equal noncitation words at original positions on both sides",
                    "minimum_and_density_numerator": "Noncitation equal pairs only",
                    "gap_and_span_accounting": "Original word positions, including intervening citations",
                },
                "normalization_version": NORMALIZATION_VERSION, "source_time_limit_seconds": source_seconds,
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
                         "manuscript_ledger_sha256": ledger_sha256(mt),
                         "similar_diagnostics": [m["diagnostics"] for m in matches + raw if m["match_kind"] == "similar"],
                         "alignment_diagnostics": [m["diagnostics"] for m in matches + raw],
                         "calibration_ready": bool(coverage) and denominator > 0 and all(r["status"] == "compared" for r in coverage),
                         "audit_complete": bool(coverage) and all(
                             r["audit_search_complete"] or r["status"] in {"excluded-by-user", "excluded-identical"}
                             for r in coverage),
                         "search": "Restored v1 three-word-seeded ordered alternatives; citations retain coordinates but cannot qualify Similar pairs."},
        "methodology": {
            "name": MODEL_ID, "version": VERSION, "normalization": NORMALIZATION_VERSION,
            "exact": "Contiguous equal normalized word units, at least nine.",
            "similar": "At least nine noncitation equal words, exact three-word retrieval seed, gap at most five per side and global density at least 60% per side; citation positions remain in gap/span denominators.",
            "denominator": DENOMINATOR_DESCRIPTION,
            "limitations": "Lexical hypotheses, not vendor parameters. No seed means no candidate. Literal numbers; no citation removal. Resource-limited search is explicitly partial.",
        },
    }
