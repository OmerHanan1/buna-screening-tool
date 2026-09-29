"""Candidate engine 'classified-v1': separate EXACT and SIMILAR wording paths.

Explicit opt-in only. Match type (exact/similar) is independent of the
attribution axis (quotation/citation flags). Every numeric setting below is a Buna
hypothesis, not a published Crossref/iThenticate parameter.

EXACT: maximal contiguous runs of equal normalized word units (>= min_exact_words),
found by hashing source word n-grams and extending seeds; no insertion, deletion,
substitution or reordering inside a run. Independent of every similar-path setting.

SIMILAR: within one manuscript sentence, a local source span sharing enough specific
wording, allowing a small number of word edits and bounded reordering of shared blocks.
Only actually equal words are reported as matched spans.

Per-word precedence: excluded > exact > similar > unmatched (only if every source was
fully checked) / not-fully-checked. States are emitted as intervals, not per word.
"""
from __future__ import annotations

import hashlib
import math
import re
import time
import unicodedata
from bisect import bisect_right
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Callable

from buna.comparison import (_BOILERPLATE, _citation_context, _compound_spellings, _intervals, _line_join_words,
                             _manuscript_view, _mask, _passage, _quotation_intervals, MAX_EVIDENCE_BYTES,
                             MAX_SOURCE_SECONDS, MAX_DOCUMENT_CHARACTERS, ComparisonCancelled)
from buna.documents import abstract_start, SOURCE_MAX_CHARACTERS

VERSION = "classified-v1.1"
NORMALIZATION_VERSION = "nfkc-casefold-combining-numeric-v1"
WORKING_INDEX_BYTES = 128 * 1024 * 1024
DEFAULT_CONFIG = {
    "min_exact_words": 9,          # report profile "matches less than 9 words" (configurable)
    "similar": {
        "enabled": True,
        "min_shared_words": 9,     # actually equal words in the local span
        "min_shared_content_words": 5,
        "max_word_edits": 2,       # unmatched words allowed on the longer side ...
        "max_edit_fraction": 0.15, # ... or this share of the longer span, whichever is larger,
        "max_word_edits_ceiling": 4,  # ... but never more than this hard ceiling
        "max_out_of_order_blocks": 2,
        "min_reordered_block_words": 2,
        "min_shared_bigrams": 3,   # retrieval only: every source sentence meeting it is evaluated
    },
}
STOPWORDS = frozenset((
    "a an and are as at be been being but by can could did do does done for from had has have having he her "
    "hers him his how i if in into is it its itself may might more most must no nor not of off on once only or "
    "other our ours out over own same she should so some such than that the their theirs them then there these "
    "they this those through to too under until up very was we were what when where which while who whom why "
    "will with would you your also both each few further here just via per vs al et"
).split())
_COMBINING = "\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"
_WORD = re.compile(rf"\b\d+(?:[.,]\d+)+\b|[^\W_][{_COMBINING}]*(?:(?:['’][^\W_]|[^\W_])[{_COMBINING}]*)*", re.UNICODE)
_SENTENCE_END = re.compile(r"[.!?][\"”’)\]]*(?=\s+[\"“(\[]?[A-Z0-9])")
_ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|et al|vs|cf|etc|approx|Fig|Figs|Eq|No|pp|p|Dr|Prof|Mr|Ms|Vol|St)|\b[A-Z])\.$")


class _Limit(Exception):
    """A declared resource guard stopped work; results are partial."""

    def __init__(self, kind: str, reason: str):
        super().__init__(reason)
        self.kind, self.reason = kind, reason


def normalize(word: str) -> str:
    return unicodedata.normalize("NFKC", word).casefold().replace("’", "'")


def tokens(text: str, join_words: set[str] | None = None, check=lambda: None) -> list[tuple[str, int, int]]:
    """Word units with original offsets; combining marks stay inside words.

    Line-break hyphenation and soft hyphens join exactly as in the production tokenizer.
    """
    out = []
    for number, match in enumerate(_WORD.finditer(text)):
        if number % 2048 == 0:
            check()
        word = normalize(match.group())
        if out:
            previous, start, end = out[-1]
            gap = text[end:match.start()]
            if previous[-1].isalpha() and match.group()[0].islower() and (
                gap == "\u00ad" or (previous + word in (join_words or set()) and re.fullmatch(r"-[ \t]*\r?\n[ \t]*", gap))):
                out[-1] = (previous + word, start, match.end())
                continue
        out.append((word, match.start(), match.end()))
    return out


def sentences(text: str, toks: list[tuple]) -> list[int]:
    """Sentence id per token; paragraph breaks and sentence punctuation split."""
    cuts = [m.end() for m in _SENTENCE_END.finditer(text) if not _ABBREV.search(text[max(0, m.start() - 8):m.start() + 1])]
    cuts += [m.start() for m in re.finditer(r"\n[ \t]*\n", text)]
    cuts.sort()
    return [bisect_right(cuts, start) for _, start, _ in toks]


def _regions(*masks: list[bool]) -> list[int]:
    out, region, previous = [], 0, None
    for state in zip(*masks):
        if previous is not None and state != previous:
            region += 1
        out.append(region)
        previous = state
    return out


def _content(word: str) -> bool:
    return word.isalpha() and len(word) >= 3 and word not in STOPWORDS


def _page_of(document: dict, offset: int):
    cursor = 0
    for page in document.get("pages", []):
        if cursor <= offset < cursor + len(page.get("text", "")) + 2:
            return page.get("number")
        cursor += len(page.get("text", "")) + 2
    return None


def _spans(toks, positions):
    """Merge equal-word positions into [char_start, char_end] ranges (only equal words)."""
    spans = []
    for index in sorted(positions):
        start, end = toks[index][1], toks[index][2]
        if spans and index == spans[-1][2] + 1:
            spans[-1][1], spans[-1][2] = end, index
        else:
            spans.append([start, end, index])
    return [[a, b] for a, b, _ in spans]


def _exact_runs(mw, m_ok, m_region, sw, s_ok, s_region, k, check):
    """Maximal equal runs >= k words. Seeds: hashed k-grams; extension both directions."""
    table = defaultdict(list)
    index_bytes = 0
    for j in range(len(sw) - k + 1):
        if j % 5000 == 0:
            check()
        if all(s_ok[j:j + k]) and s_region[j] == s_region[j + k - 1]:
            index_bytes += 192
            if index_bytes > WORKING_INDEX_BYTES:
                raise _Limit("index-memory-limit", "Exact retrieval index reached its bounded working-memory budget.")
            table[hash(tuple(sw[j:j + k]))].append(j)
    reached = {}
    for i in range(len(mw) - k + 1):
        if i % 2000 == 0:
            check()
        if not (m_ok[i] and m_ok[i + k - 1] and m_region[i] == m_region[i + k - 1]):
            continue
        for n, j in enumerate(table.get(hash(tuple(mw[i:i + k])), ())):
            if n and n % 1000 == 0:
                check()
            diagonal = j - i
            if reached.get(diagonal, -1) > i or mw[i:i + k] != sw[j:j + k]:
                continue
            a, b = i, j
            while a > 0 and b > 0 and m_ok[a - 1] and s_ok[b - 1] and m_region[a - 1] == m_region[i] \
                    and s_region[b - 1] == s_region[j] and mw[a - 1] == sw[b - 1]:
                if (i - a) % 1024 == 0:
                    check()
                a, b = a - 1, b - 1
            e, f = i + k, j + k
            while e < len(mw) and f < len(sw) and m_ok[e] and s_ok[f] and m_region[e] == m_region[i] \
                    and s_region[f] == s_region[j] and mw[e] == sw[f]:
                if (e - i) % 1024 == 0:
                    check()
                e, f = e + 1, f + 1
            if diagonal not in reached and index_bytes + (len(reached) + 1) * 96 > WORKING_INDEX_BYTES:
                raise _Limit("index-memory-limit", "Exact retrieval tracking reached its bounded working-memory budget.")
            reached[diagonal] = e
            yield a, e, b, f


def _similar(unit, mw, m_ok, m_region, sw, s_ok, s_sentence, s_bigrams, s_sentence_tokens, s_region, cfg, exact_pairs, check):
    """Best local similar-wording alignment for one manuscript sentence in one source."""
    positions = [i for i in unit if m_ok[i]]
    if len(positions) < cfg["min_shared_words"]:
        return None, 0
    # Split at exclusion-region changes; take the region with most eligible words.
    groups = defaultdict(list)
    for i in positions:
        groups[m_region[i]].append(i)
    positions = max(groups.values(), key=len)
    words = [mw[i] for i in positions]
    hits = Counter()
    for a, b in zip(words, words[1:]):
        check()
        for sid in s_bigrams.get((a, b), ()):
            hits[sid] += 1
            if len(hits) * 96 > WORKING_INDEX_BYTES:
                raise _Limit("index-memory-limit", "Similar candidate index reached its bounded working-memory budget.")
    best = None
    evaluated = 0
    for sid, count in sorted(hits.items(), key=lambda x: (-x[1], x[0])):
        if count < cfg["min_shared_bigrams"]:
            break
        evaluated += 1
        check()
        for span in ((sid,), (sid - 1, sid), (sid, sid + 1)):
            source = [j for s in span for j in s_sentence_tokens.get(s, ()) if s_ok[j]]
            if len(source) < cfg["min_shared_words"]:
                continue
            regions = defaultdict(list)
            for j in source:
                regions[s_region[j]].append(j)
            for region in regions.values():
                check()
                if len(region) < cfg["min_shared_words"]:
                    continue
                # difflib's inner search has no cooperative cancellation callback.
                # Bound one atomic alignment before invoking it, never slice away text.
                if len(words) * len(region) > 2_000_000:
                    raise _Limit("alignment-work-limit", "One similar alignment exceeds the bounded atomic-work budget; source is partly checked.")
                candidate = _align(positions, words, region, [sw[j] for j in region], cfg, check)
                if candidate and (best is None or candidate["score"] > best["score"]):
                    best = candidate
    if best is None or set(best["pairs"]) <= exact_pairs:
        return None, evaluated
    return best, evaluated


def _lis_members(values):
    """Indices of one longest strictly increasing subsequence (deterministic)."""
    tails, previous = [], [None] * len(values)
    for n, value in enumerate(values):
        k = bisect_right([values[t] for t in tails], value - 1e-9)
        if k:
            previous[n] = tails[k - 1]
        if k == len(tails):
            tails.append(n)
        else:
            tails[k] = n
    members, node = set(), tails[-1] if tails else None
    while node is not None:
        members.add(node)
        node = previous[node]
    return members


def _align(m_pos, m_words, s_pos, s_words, cfg, check=lambda: None):
    """Best contiguous local alignment of one-to-one equal-word blocks.

    Blocks: difflib's order-preserving blocks (any size) plus remaining common runs of at
    least ``min_reordered_block_words`` in any order (never single-word bag matches).
    Every contiguous range of blocks (in manuscript order) is scored; blocks outside the
    longest source-ordered subsequence count as reordered and must each have >= the minimum
    block size; the span's unmatched words must stay within the edit allowance.
    """
    used_m, used_s, blocks = set(), set(), []
    check()
    for block in SequenceMatcher(None, m_words, s_words, autojunk=False).get_matching_blocks():
        if block.size:
            blocks.append((block.a, block.b, block.size))
            used_m.update(range(block.a, block.a + block.size))
            used_s.update(range(block.b, block.b + block.size))
    minimum = cfg["min_reordered_block_words"]
    while True:
        check()
        best = None
        for a in range(len(m_words)):
            if a % 32 == 0:
                check()
            if a in used_m:
                continue
            for b in range(len(s_words)):
                if b % 512 == 0:
                    check()
                if b in used_s or m_words[a] != s_words[b]:
                    continue
                n = 0
                while a + n < len(m_words) and b + n < len(s_words) and a + n not in used_m \
                        and b + n not in used_s and m_words[a + n] == s_words[b + n]:
                    n += 1
                if n >= minimum and (best is None or n > best[2]):
                    best = (a, b, n)
        if best is None:
            break
        blocks.append(best)
        used_m.update(range(best[0], best[0] + best[2]))
        used_s.update(range(best[1], best[1] + best[2]))
    blocks.sort()
    chosen = None
    for i in range(len(blocks)):
        check()
        for j in range(i, len(blocks)):
            if j % 32 == 0:
                check()
            chain = blocks[i:j + 1]
            order = _lis_members([b for _, b, _ in chain])
            kept = [block for n, block in enumerate(chain) if n in order or block[2] >= minimum]
            if not kept or kept[0] != chain[0] or kept[-1] != chain[-1]:
                continue  # range endpoints must be real matches
            reordered = sum(1 for n, block in enumerate(chain) if n not in order and block[2] >= minimum)
            if reordered > cfg["max_out_of_order_blocks"]:
                continue
            shared = sum(n for _, _, n in kept)
            if shared < cfg["min_shared_words"]:
                continue
            content = sum(_content(m_words[a + t]) for a, _, n in kept for t in range(n))
            if content < cfg["min_shared_content_words"]:
                continue
            m_span = kept[-1][0] + kept[-1][2] - kept[0][0]
            s_lo = min(b for _, b, _ in kept); s_hi = max(b + n for _, b, n in kept)
            s_span = s_hi - s_lo
            m_unmatched, s_unmatched = m_span - shared, s_span - shared
            edits = max(m_unmatched, s_unmatched)
            allowed = min(cfg["max_word_edits_ceiling"],
                          max(cfg["max_word_edits"], math.floor(cfg["max_edit_fraction"] * max(m_span, s_span))))
            if edits > allowed:
                continue
            similarity = 2 * shared / (m_span + s_span)
            candidate = {"kept": kept, "shared": shared, "content": content, "m_unmatched": m_unmatched,
                         "s_unmatched": s_unmatched, "edits": edits, "allowed_edits": allowed,
                         "blocks": len(kept), "out_of_order_blocks": reordered,
                         "similarity": similarity, "score": (shared, similarity, -reordered)}
            if chosen is None or candidate["score"] > chosen["score"]:
                chosen = candidate
    if chosen is None:
        return None
    chosen["pairs"] = sorted((m_pos[a + t], s_pos[b + t]) for a, b, n in chosen.pop("kept") for t in range(n))
    return chosen


def _side(document, toks, positions, lo, hi):
    pages, cursor = [], 0
    for page in document.get("pages", []):
        end = cursor + len(page.get("text", ""))
        if cursor < toks[hi][2] and end > toks[lo][1]:
            pages.append(page.get("number"))
        cursor = end + 2
    return {"start": toks[lo][1], "end": toks[hi][2], "word_start": lo, "word_end": hi + 1,
            "page": _page_of(document, toks[lo][1]),
            "pages": pages,
            "equal_spans": _spans(toks, positions),
            "text": document["text"][toks[lo][1]:toks[hi][2]]}


def _attribution(manuscript, mt, source, positions, quoted, uncertain):
    """Existing citation/quotation context rules, applied on original offsets (independent axis)."""
    lo, hi = min(positions), max(positions)
    passage = _passage(manuscript, mt, lo, hi + 1, set(positions))
    citation = _citation_context(passage["text"], manuscript, source)
    quoted_words = sum(quoted[i] for i in positions)
    flags = (["quotation-context"] if quoted_words else []) + (["citation-context"] if citation["present"] else []) + \
        (["possible-boilerplate"] if _BOILERPLATE.search(passage["text"]) else [])
    classification = ("Cited quotation" if citation["present"] and quoted_words else
                      "Quotation—citation may be missing" if quoted_words else
                      "Cited wording—quotation may be needed" if citation["present"] else "Unattributed text overlap")
    status = "recognized" if quoted_words else "uncertain" if any(uncertain[i] for i in positions) else "not-detected"
    return passage, {"citation": citation, "flags": flags, "classification": classification,
                     "quotation": {"status": status, "matched_quoted_words": quoted_words}}


def classify_documents(manuscript: dict, sources: list[dict], **kwargs) -> dict:
    """Classify manuscript text against sources (see ``_classify``)."""
    result = _classify(manuscript, sources, **kwargs)
    result.pop("_tokens")
    return result


def _classify(manuscript: dict, sources: list[dict], *, config: dict | None = None,
                       exclude_quotes: bool = True, manuscript_scope: str = "abstract-onward",
                       load_document: Callable[[dict], dict] | None = None,
                       cancelled: Callable[[], bool] | None = None,
                       source_seconds: float = MAX_SOURCE_SECONDS,
                       total_time_limit_seconds: float | None = None,
                       progress: Callable[[str], None] | None = None,
                       source_progress: Callable[[dict], None] | None = None) -> dict:
    """Classify manuscript text against sources. Same resource contract as compare_documents:
    per-source time limit, optional total deadline (0 < t <= 600 s), cancellation, evidence guard.
    Sources not reached before the deadline are 'skipped-time-limit'; text they could have
    matched is 'not-fully-checked', never 'unmatched'."""
    if total_time_limit_seconds is not None and not 0 < total_time_limit_seconds <= 600:
        raise ValueError("Total comparison time limit must be positive and at most 600 seconds.")
    deadline = time.monotonic() + total_time_limit_seconds if total_time_limit_seconds is not None else None
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    cfg["similar"] = {**DEFAULT_CONFIG["similar"], **(config or {}).get("similar", {})}
    k = int(cfg["min_exact_words"])
    if not 1 <= k <= 120:
        raise ValueError("Exact minimum must be between 1 and 120 word units.")

    def check():
        if cancelled and cancelled():
            raise ComparisonCancelled("Comparison cancelled.")

    text = manuscript.get("text", "")
    if len(text) > MAX_DOCUMENT_CHARACTERS:
        raise ValueError("Manuscript exceeds the 1,000,000-character comparison limit.")
    warnings = list(manuscript.get("warnings", []))
    join = _line_join_words([text])
    compounds = _compound_spellings(text)
    load_errors = {}
    normalization_complete = True
    for source in sources:
        check()
        if deadline is not None and time.monotonic() >= deadline:
            normalization_complete = False
            break
        document = None
        if not source.get("excluded"):
            try:
                document = load_document(source) if load_document else source.get("document")
            except (OSError, ValueError) as exc:
                load_errors[str(source.get("id"))] = str(exc)
        if document and document.get("text"):
            if len(document["text"]) <= SOURCE_MAX_CHARACTERS:
                join.update(_line_join_words([text, document["text"]]))
                compounds.update(_compound_spellings(document["text"]))
        del document
    join -= compounds
    mt = tokens(text, join, check)
    mw = [t[0] for t in mt]
    bib = _mask(mt, _intervals(manuscript))
    quote_spans, uncertain_spans = _quotation_intervals(text)
    quoted = _mask(mt, quote_spans)
    uncertain = _mask(mt, sorted(uncertain_spans))
    scope = {"requested": manuscript_scope, "applied": "whole-document", "start_offset": None,
             "start_page": None, "heading_text": None}
    front = [False] * len(mt)
    if manuscript_scope == "abstract-onward":
        found = abstract_start(manuscript)
        if found["start_offset"] is not None:
            scope.update(found, applied="abstract-onward")
            front = [t[1] < found["start_offset"] for t in mt]
            warnings.append(f"Analysis starts at the Abstract heading on page {found['start_page']}; {sum(front)} preceding front-matter words were excluded from matching and the score denominator.")
        else:
            warnings.append("Abstract heading not detected; the whole manuscript was analyzed (front matter was not excluded).")
    in_scope = [not b and not f for b, f in zip(bib, front)]
    m_region = _regions([not s for s in in_scope], quoted if exclude_quotes else [False] * len(mt))
    m_units = defaultdict(list)
    for index, sid in enumerate(sentences(text, mt)):
        m_units[sid].append(index)
    fingerprint = hashlib.sha256(" ".join(mw).encode()).digest()

    matches, coverage = [], []
    evidence = {"bytes": 0}
    exact_words, similar_words = defaultdict(set), defaultdict(set)

    def reserve(size):
        if evidence["bytes"] + size > MAX_EVIDENCE_BYTES:
            raise _Limit("evidence-memory-limit", f"Stopped at the {MAX_EVIDENCE_BYTES // 2**20} MiB estimated "
                         "evidence-memory limit; results are partial.")
        evidence["bytes"] += size

    def finish_row(row):
        if source_progress:
            source_progress(row)

    for number, source in enumerate(sources):
        check()
        source_id = str(source.get("id", f"source-{number + 1}"))
        if source.get("excluded"):
            coverage.append({"source_id": source_id, "status": "excluded-by-user"})
            finish_row(coverage[-1])
            continue
        if not normalization_complete or (deadline is not None and time.monotonic() >= deadline):
            coverage.append({"source_id": source_id, "status": "skipped-time-limit", "limits_reached": ["total-time-limit"],
                             "reason": "The total comparison time budget ended before this source."})
            finish_row(coverage[-1])
            continue
        started = time.monotonic()
        try:
            document = load_document(source) if load_document else source.get("document")
        except (OSError, ValueError) as exc:
            document = None
            load_errors[source_id] = str(exc)
        if not document or not document.get("text"):
            coverage.append({"source_id": source_id, "status": "unavailable",
                             "reason": load_errors.get(source_id, "No readable extracted text.")})
            finish_row(coverage[-1])
            continue
        if len(document["text"]) > SOURCE_MAX_CHARACTERS:
            coverage.append({"source_id": source_id, "status": "skipped-size-limit",
                             "reason": "Source exceeds the 2,000,000-character comparison limit."})
            finish_row(coverage[-1])
            del document
            continue
        row = {"source_id": source_id, "status": "compared", "exact_matches": 0, "similar_matches": 0,
               "similar_candidates_evaluated": 0, "limits_reached": [],
               "exact_scan_complete": False, "similar_scan_complete": False}
        coverage.append(row)

        def budget():
            check()
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                raise _Limit("total-time-limit", "The total comparison time budget ended during this source; "
                             "retained matches are partial.")
            if now - started >= source_seconds:
                raise _Limit("source-time-limit", f"Stopped at the {source_seconds:g}-second per-source limit; "
                             "results for this source are partial.")

        def record(kind, m_positions, s_positions, extra):
            budget()
            # Reserve before constructing passage strings, spans, pairs or attribution.
            reserve(4096 + 128 * len(m_positions) + 4 * (
                mt[max(m_positions)][2] - mt[min(m_positions)][1]
                + st[max(s_positions)][2] - st[min(s_positions)][1]))
            passage, attribution = _attribution(manuscript, mt, source, m_positions, quoted, uncertain)
            excluded = bool(exclude_quotes and quoted[m_positions[0]])
            manuscript_side = _side(manuscript, mt, m_positions, min(m_positions), max(m_positions))
            manuscript_side["context"] = {"start": passage["start"], "end": passage["end"], "section": passage["section"]}
            matches.append({"source_id": source_id, "match_kind": kind,
                  "manuscript": manuscript_side,
                  "source": _side(document, st, s_positions, min(s_positions), max(s_positions)),
                  "aligned_pairs": [list(pair) for pair in zip(m_positions, s_positions)],
                  "excluded_from_score": excluded, "exclusion_reason": "recognized-quotation" if excluded else None,
                  **attribution, **extra})
            if not excluded:
                (exact_words if kind == "exact" else similar_words)[source_id].update(m_positions)
            row[kind + "_matches"] += 1

        try:
            budget()
            st = tokens(document["text"], join, budget)
            sw = [t[0] for t in st]
            if hashlib.sha256(" ".join(sw).encode()).digest() == fingerprint:
                row["status"] = "excluded-identical"
                finish_row(row)
                del document
                continue
            s_ok = [not b for b in _mask(st, _intervals(document))]
            s_region = _regions([not ok for ok in s_ok])
            if progress:
                progress(f"Classifying source {number + 1}/{len(sources)}: exact wording.")
            exact_pairs = set()
            for a, e, b, f in _exact_runs(mw, in_scope, m_region, sw, s_ok, s_region, k, budget):
                if (len(exact_pairs) + e - a) * 128 > WORKING_INDEX_BYTES:
                    raise _Limit("index-memory-limit", "Exact pair tracking reached its bounded working-memory budget.")
                record("exact", range(a, e), range(b, f),
                       {"matched_words": e - a, "similarity": 1.0,
                        "edits": {"manuscript_unmatched": 0, "source_unmatched": 0, "counted": 0, "allowed": 0},
                        "order": {"blocks": 1, "out_of_order_blocks": 0}})
                exact_pairs.update((a + t, b + t) for t in range(e - a))
            row["exact_scan_complete"] = True
            if cfg["similar"]["enabled"]:
                if progress:
                    progress(f"Classifying source {number + 1}/{len(sources)}: similar wording.")
                s_sentence = sentences(document["text"], st)
                s_sentence_tokens, s_bigrams = defaultdict(list), defaultdict(set)
                index_bytes = 0
                for j, sid in enumerate(s_sentence):
                    if j % 5000 == 0:
                        budget()
                    index_bytes += 48
                    if index_bytes > WORKING_INDEX_BYTES:
                        raise _Limit("index-memory-limit", "Similar sentence index reached its working-memory budget.")
                    s_sentence_tokens[sid].append(j)
                for j in range(len(sw) - 1):
                    if j % 5000 == 0:
                        budget()
                    if s_ok[j] and s_ok[j + 1] and s_sentence[j] == s_sentence[j + 1]:
                        pair = (sw[j], sw[j + 1])
                        if s_sentence[j] not in s_bigrams.get(pair, ()):
                            index_bytes += 320
                            if index_bytes > WORKING_INDEX_BYTES:
                                raise _Limit("index-memory-limit", "Similar bigram index reached its working-memory budget.")
                            s_bigrams[pair].add(s_sentence[j])
                for unit in m_units.values():
                    budget()
                    # Skipped only when THIS source already matches every word exactly;
                    # other sources are always searched.
                    if all(i in exact_words[source_id] or not in_scope[i] for i in unit):
                        continue
                    best, evaluated = _similar(unit, mw, in_scope, m_region, sw, s_ok, s_sentence, s_bigrams,
                                               s_sentence_tokens, s_region, cfg["similar"], exact_pairs, budget)
                    row["similar_candidates_evaluated"] += evaluated
                    if best:
                        record("similar", [p[0] for p in best["pairs"]], [p[1] for p in best["pairs"]],
                               {"matched_words": best["shared"], "similarity": round(best["similarity"], 4),
                                "edits": {"manuscript_unmatched": best["m_unmatched"], "source_unmatched": best["s_unmatched"],
                                          "counted": best["edits"], "allowed": best["allowed_edits"]},
                                "order": {"blocks": best["blocks"], "out_of_order_blocks": best["out_of_order_blocks"]},
                                "shared_content_words": best["content"]})
            row["similar_scan_complete"] = True
        except _Limit as limit:
            row.update(status="compared-with-limits", reason=limit.reason)
            row["limits_reached"].append(limit.kind)
        finish_row(row)
        del document

    all_exact = set().union(*exact_words.values()) if exact_words else set()
    all_similar = (set().union(*similar_words.values()) if similar_words else set()) - all_exact
    complete = bool(any(r["status"] == "compared" for r in coverage)) and all(
        r["status"] in ("compared", "excluded-by-user", "excluded-identical") for r in coverage)
    per_word_sources = defaultdict(set)
    for sid, words in exact_words.items():
        for w in words:
            per_word_sources[w].add(sid)
    for sid, words in similar_words.items():
        for w in words:
            per_word_sources[w].add(sid)

    def state(i):
        if front[i]:
            return "excluded-front-matter"
        if bib[i]:
            return "excluded-bibliography"
        if exclude_quotes and quoted[i]:
            return "excluded-quotation"
        if i in all_exact:
            return "exact"
        if i in all_similar:
            return "similar"
        return "unmatched" if complete else "not-fully-checked"

    intervals = []
    for i in range(len(mt)):
        current = (state(i), tuple(sorted(per_word_sources.get(i, ()))))
        if intervals and intervals[-1]["key"] == current and intervals[-1]["word_end"] == i:
            intervals[-1]["word_end"], intervals[-1]["end"] = i + 1, mt[i][2]
        else:
            intervals.append({"key": current, "state": current[0], "sources": list(current[1]),
                              "word_start": i, "word_end": i + 1, "start": mt[i][1], "end": mt[i][2]})
    for item in intervals:
        item.pop("key")
    counts = Counter(state(i) for i in range(len(mt)))
    analyzed = len(mt) - counts["excluded-front-matter"]
    pct = lambda n: round(100 * n / analyzed, 2) if analyzed else 0.0
    per_source = {sid: len(exact_words.get(sid, set()) | similar_words.get(sid, set())) for sid in
                  {r["source_id"] for r in coverage}}
    for row in coverage:
        if row["status"] in ("compared", "compared-with-limits"):
            row["overlapping_words"] = per_source.get(row["source_id"], 0)
            row["exact_words"] = len(exact_words.get(row["source_id"], ()))
            row["similar_only_words"] = len(similar_words.get(row["source_id"], set()) - exact_words.get(row["source_id"], set()))
    return {
        "engine_candidate": VERSION, "normalization_version": NORMALIZATION_VERSION,
        "config": cfg, "exclude_quotes": exclude_quotes, "manuscript_scope": scope,
        "total_time_limit_seconds": total_time_limit_seconds,
        "warnings": warnings,
        "metrics": {"total_words": len(mt), "analyzed_words": analyzed,
                    "exact_words": len(all_exact), "similar_only_words": len(all_similar),
                    "combined_words": len(all_exact) + len(all_similar),
                    "exact_percent": pct(len(all_exact)), "similar_only_percent": pct(len(all_similar)),
                    "combined_percent": pct(len(all_exact) + len(all_similar)),
                    "unmatched_words": counts["unmatched"], "not_fully_checked_words": counts["not-fully-checked"],
                    "quotation_words": sum(q and eligible for q, eligible in zip(quoted, in_scope)),
                    "excluded_words": {"front_matter": counts["excluded-front-matter"],
                                       "bibliography": counts["excluded-bibliography"],
                                       "quotation": counts["excluded-quotation"]},
                    "all_sources_fully_checked": complete},
        "matches": sorted(matches, key=lambda m: (m["manuscript"]["start"], m["source_id"], m["source"]["start"],
                                                  m["match_kind"])),
        "coverage_intervals": intervals, "source_coverage": coverage,
        "labels": {"unmatched": "No match found in the checked sources (not a finding of originality).",
                   "not-fully-checked": "Not fully checked: at least one source was unavailable, skipped or only partly compared.",
                   "similar": "Similar wording (shared words with small edits or reordering); not a paraphrase or plagiarism finding."},
        "_tokens": mt,
    }


def classify_report(manuscript: dict, sources: list[dict], **kwargs) -> dict:
    """Opt-in adapter: compare_documents-shaped report plus an additive ``classification`` block.

    Existing consumers read matches/metrics/source_coverage/manuscript_pages unchanged;
    ``kind`` keeps legacy values (exact / near-verbatim) while ``match_kind`` is authoritative.
    Legacy score fields use the combined exact+similar word union on the scoped denominator.
    """
    result = _classify(manuscript, sources, **kwargs)
    mt = result.pop("_tokens")
    metrics = result["metrics"]
    matches = []
    per_word = defaultdict(set)
    for m in result["matches"]:
        for a, _ in m["aligned_pairs"]:
            per_word[a].add(m["source_id"])
    evidence = []
    numbers = {str(s.get("id", f"source-{i + 1}")): s.get("source_number", i + 1) for i, s in enumerate(sources)}
    for m in result["matches"]:
        positions = {a for a, _ in m["aligned_pairs"]}
        scored = sorted(positions) if not m["excluded_from_score"] else []
        side = m["manuscript"]
        passage = _passage(manuscript, mt, side["word_start"], side["word_end"], positions)
        s_doc_text = m["source"]
        legacy = {**m, "kind": "exact" if m["match_kind"] == "exact" else "near-verbatim",
                  "manuscript": {**passage, "equal_spans": side["equal_spans"]},
                  "source": {**s_doc_text, "match_start": s_doc_text["start"], "match_end": s_doc_text["end"],
                             "highlights": [[a - s_doc_text["start"], b - s_doc_text["start"]] for a, b in s_doc_text["equal_spans"]]},
                  "included_words": len(scored), "excluded_words": len(positions) - len(scored),
                  "scored_word_positions": scored, "source_number": numbers.get(m["source_id"]),
                  "alternative_source_ids": sorted(set().union(*(per_word[p] for p in positions)) - {m["source_id"]}),
                  "exclusion_reasons": [m["exclusion_reason"]] if m["exclusion_reason"] else []}
        matches.append(legacy)
        evidence.append((legacy, positions))
    covered = {p for m in matches for p in m["scored_word_positions"]}
    denominator = metrics["analyzed_words"]
    eligible = denominator - metrics["excluded_words"]["bibliography"] - metrics["excluded_words"]["quotation"]
    basis = ("abstract-onward-word-units" if result["manuscript_scope"]["applied"] == "abstract-onward"
             else "all-submitted-word-units")
    for row in result["source_coverage"]:
        if "overlapping_words" in row:
            row.update(eligible_words=eligible, score_denominator_words=denominator,
                       overlap_percent=round(100 * row["overlapping_words"] / denominator, 2) if denominator else 0.0)
    warnings = result["warnings"] + [
        "Experimental exact/similar wording model. Accuracy and Crossref equivalence are not established; scores may differ from the standard model.",
        "Exact means contiguous equal normalized word units. Similar means shared wording with bounded edits or block reordering, not semantic paraphrase detection.",
    ]
    if not metrics["all_sources_fully_checked"]:
        warnings.append("Not all sources were fully checked. Unmarked text is not fully checked, not a finding of originality.")
    return {
        "algorithm_version": VERSION, "comparison_model": VERSION,
        "quality_notice": "Candidate engine: exact and similar wording are classified separately; not vendor-equivalent.",
        "settings": {"minimum_matched_words": result["config"]["min_exact_words"], "exclude_bibliography": True,
                     "exclude_quotes": result["exclude_quotes"], "score_basis": basis,
                     "manuscript_scope": result["manuscript_scope"], "classification_config": result["config"],
                     "total_time_limit_seconds": result["total_time_limit_seconds"],
                     "normalization_version": NORMALIZATION_VERSION, "working_index_limit_mib": 128,
                     "source_time_limit_seconds": kwargs.get("source_seconds", MAX_SOURCE_SECONDS)},
        "metrics": {"total_words": metrics["total_words"], "eligible_words": eligible,
                    "score_denominator_words": denominator, "analyzed_words": denominator,
                    "front_matter_words": metrics["excluded_words"]["front_matter"],
                    "score_basis": basis, "score_policy_version": VERSION,
                    "overlapping_words": len(covered),
                    "overlap_percent": round(100 * len(covered) / denominator, 2) if denominator else 0.0,
                    "bibliography_words": metrics["excluded_words"]["bibliography"],
                    "quotation_words": metrics["quotation_words"],
                    "excluded_quotation_words": metrics["excluded_words"]["quotation"],
                    "unscorable": denominator == 0, "all_text_excluded": eligible == 0 and denominator > 0,
                    "sources_supplied": len(sources),
                    "sources_compared": sum(r["status"] in ("compared", "compared-with-limits") for r in result["source_coverage"]),
                    "identical_sources_excluded": sum(r["status"] == "excluded-identical" for r in result["source_coverage"]),
                    "truncated": not metrics["all_sources_fully_checked"],
                    "exact_words": metrics["exact_words"], "similar_only_words": metrics["similar_only_words"],
                    "exact_percent": metrics["exact_percent"], "similar_only_percent": metrics["similar_only_percent"]},
        "matches": matches, "warnings": warnings, "source_coverage": result["source_coverage"],
        "methodology": {
            "name": "Experimental exact and similar wording",
            "version": VERSION, "normalization": NORMALIZATION_VERSION,
            "exact": "Maximal contiguous equal normalized word runs, at least nine words under the default configuration.",
            "similar": "Best local equal-word block alignment per manuscript sentence/source under the declared edit/order limits; not semantic paraphrase detection.",
            "denominator": "The saved manuscript scope's normalized word units; exclusions affect the numerator. This differs from proprietary tokenizers.",
            "unmatched": "No match found by this model in fully checked sources, never proof of originality.",
            "limitations": "Similar retrieval is lexical and sentence-bounded. Only the best similar alignment per source and manuscript sentence is retained; distinct exact locations are retained.",
            "integration_corrections": "Version1.1 adds lazy loading, size/index/atomic-work guards, source exclusion boundaries, complete numeric units and warning/coverage fixes. Earlier benchmark results do not establish this revision's accuracy.",
        },
        "excluded_source_ids": [r["source_id"] for r in result["source_coverage"] if r["status"] == "excluded-identical"],
        "manuscript_pages": _manuscript_view(manuscript, mt, evidence, covered, numbers),
        "classification": {k: result[k] for k in ("engine_candidate", "normalization_version", "coverage_intervals", "labels")}
                          | {"metrics": metrics},
    }
