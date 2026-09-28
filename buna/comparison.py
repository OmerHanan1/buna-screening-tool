"""Bounded lexical evidence discovery; never a plagiarism or semantic classifier."""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Callable
from buna.local_alignment import DEFAULT_POLICY, merge_instances, ordered_instances
from buna.exclusions import quotation_intervals
from buna.documents import _REFERENCE_HEADING, abstract_start, SOURCE_MAX_CHARACTERS

ALGORITHM_VERSION = "2.5.3"
SHINGLE_WORDS = 2
MIN_EXACT_WORDS = 9
MIN_NEAR_WORDS = 9
NEAR_THRESHOLD = 0.82
WINDOW_WORDS = 120
WINDOW_OVERLAP = 24
ANCHOR_WORDS = 6
ANCHOR_FLANK_WORDS = 8
ENDING_CREDIT_MINIMUM = 0.85
# Safety limits are independent of display pagination. Normal runs traverse every
# candidate; only a declared time or evidence-memory budget can stop traversal.
MAX_SOURCE_SECONDS = 120
MAX_EVIDENCE_BYTES = 64 * 1024 * 1024
MAX_DOCUMENT_CHARACTERS = 1_000_000
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_TYPED_WORD = re.compile(r"\b\d+(?:[.,]\d+)+\b|[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)
_CITATION = re.compile(r"\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]|\([^()\n]{0,100}\b(?:19|20)\d{2}[a-z]?\)")
_BOILERPLATE = re.compile(
    r"all rights reserved|creative commons|conflicts? of interest|"
    r"no competing interests|data (?:are|is) available|informed consent|"
    r"institutional review board|statistically significant",
    re.I,
)


class ComparisonCancelled(RuntimeError):
    """Cancellation was requested between bounded comparison units."""


class _SourceTimeLimit(RuntimeError):
    pass


def _normalize(word: str) -> str:
    return unicodedata.normalize("NFKC", word).casefold().replace("’", "'")


def _line_join_words(texts: list[str]) -> set[str]:
    candidates = set()
    compounds = set()
    for text in texts:
        candidates.update(_normalize(m.group(1) + m.group(2)) for m in
                          re.finditer(r"([^\W_]+)-[ \t]*\r?\n[ \t]*([^\W_\d]+)", text))
        compounds.update(_normalize(m.group(1) + m.group(2)) for m in
                         re.finditer(r"([^\W_]+)-([^\W_]+)", text))
    confirmed = set()
    if candidates:
        for text in texts:
            confirmed.update(_normalize(m.group()) for m in _WORD.finditer(text) if _normalize(m.group()) in candidates)
    return confirmed - compounds


def _compound_spellings(text: str) -> set[str]:
    return {_normalize(match.group(1) + match.group(2))
            for match in re.finditer(r"([^\W_]+)-([^\W_]+)", text)}


def _tokens(text: str, join_words: set[str] | None = None, *, typed: bool = False) -> list[tuple[str, int, int]]:
    tokens = []
    for match in (_TYPED_WORD if typed else _WORD).finditer(text):
        word = _normalize(match.group())
        if tokens:
            previous, start, end = tokens[-1]
            gap = text[end:match.start()]
            if previous[-1].isalpha() and match.group()[0].islower() and (
                gap == "\u00ad" or (previous + word in (join_words or set()) and
                                   re.fullmatch(r"-[ \t]*\r?\n[ \t]*", gap))
            ):
                tokens[-1] = (previous + word, start, match.end())
                continue
        tokens.append((word, match.start(), match.end()))
    return tokens


def text_fingerprint(text: str) -> str:
    return hashlib.sha256(" ".join(token[0] for token in _tokens(text)).encode()).hexdigest()


def _intervals(document: dict) -> list[tuple[int, int]]:
    return sorted(
        (segment["start"], segment["end"]) for segment in document.get("segments", [])
        if segment.get("kind") in ("reference", "bibliography")
        or _REFERENCE_HEADING.fullmatch(segment.get("section", ""))
    )


def _mask(tokens: list[tuple], intervals: list[tuple[int, int]]) -> list[bool]:
    result = [False] * len(tokens)
    cursor = 0
    for index, (_, start, end) in enumerate(tokens):
        while cursor < len(intervals) and intervals[cursor][1] <= start:
            cursor += 1
        if cursor < len(intervals) and intervals[cursor][0] < end:
            result[index] = True
    return result


def _boundary_regions(references: list[bool], quotations: list[bool]) -> list[int]:
    regions = []
    previous = None
    region = 0
    for state in zip(references, quotations):
        if previous is not None and state != previous:
            region += 1
        regions.append(region)
        previous = state
    return regions


def _windows(tokens: list[tuple], excluded: list[bool], document: dict) -> list[tuple[int, int]]:
    """Windows cross sentences, extraction segments and pages, but not references."""
    windows = []
    run = 0
    for index in range(len(tokens) + 1):
        if index == len(tokens) or excluded[index]:
            if run < index:
                for start in range(run, index, WINDOW_WORDS - WINDOW_OVERLAP):
                    end = min(start + WINDOW_WORDS, index)
                    if end - start >= SHINGLE_WORDS:
                        windows.append((start, end))
                    if end == index:
                        break
            run = index + (index < len(tokens) and excluded[index])
    return windows


def _passage(document: dict, tokens: list[tuple], start: int, end: int,
             highlighted: set[int]) -> dict:
    char_start, char_end = tokens[start][1], tokens[end - 1][2]
    match_start, match_end = char_start, char_end
    text = document["text"]
    # Add original sentence context without changing aligned-word coordinates.
    left = max(0, char_start - 600)
    preceding = list(re.finditer(r'[.!?]["”]?\s+|\n[ \t]*\n', text[left:char_start]))
    char_start = left + preceding[-1].end() if preceding else left
    following = re.search(r'[.!?](?:["”])?(?=\s|$)|\n[ \t]*\n', text[char_end:char_end + 600])
    char_end = char_end + following.end() if following else min(len(text), char_end + 600)
    for ref_start, ref_end in _intervals(document):
        if ref_end <= match_start:
            char_start = max(char_start, ref_end)
        if ref_start >= match_end:
            char_end = min(char_end, ref_start)
    segment = next((s for s in document.get("segments", [])
                    if s["start"] <= match_start < s["end"]), {})
    page = segment.get("page")
    if page is None:
        cursor = 0
        for item in document.get("pages", []):
            if cursor <= char_start < cursor + len(item.get("text", "")):
                page = item.get("number")
                break
            cursor += len(item.get("text", "")) + 2
    pages = []
    offset = 0
    for item in document.get("pages", []):
        if offset < char_end and offset + len(item.get("text", "")) > char_start:
            pages.append(item.get("number"))
        offset += len(item.get("text", "")) + 2
    return {"text": document["text"][char_start:char_end], "page": page, "pages": pages,
            "section": segment.get("section", "Unspecified"), "start": char_start, "end": char_end,
            "match_start": match_start, "match_end": match_end,
            "highlights": [[tokens[index][1] - char_start, tokens[index][2] - char_start]
                           for index in sorted(highlighted)],
            "offset_unit": "unicode-code-points"}


def _quotation_intervals(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    return quotation_intervals(text)


def _citation_context(context: str, document: dict, source: dict) -> dict:
    citations = [match.group() for match in _CITATION.finditer(context)]
    doi = str(source.get("doi") or "").lower().removeprefix("https://doi.org/").strip().rstrip(".,")
    context_dois = {match.group().lower().rstrip(".,;)]}") for match in re.finditer(r"10\.\d{4,9}/[^\s<>\"“”]+", context)}
    if doi and doi in context_dois:
        return {"present": True, "attribution": "matching-source-confirmed", "basis": "Source DOI appears in surrounding manuscript sentences."}
    if not citations:
        return {"present": False, "attribution": "not-detected", "basis": "No citation marker was recognized in the surrounding sentences; absence is not proven."}
    numbers = {int(number) for citation in citations if citation.startswith("[") for number in re.findall(r"\d+", citation)}
    for reference in document.get("references", []):
        raw = reference.get("raw", "")
        marker = re.match(r"\s*(?:\[(\d+)\]|(\d+)[.)])", raw)
        if marker and int(marker.group(1) or marker.group(2)) in numbers and doi:
            ref_doi = str(reference.get("doi") or "").lower().removeprefix("https://doi.org/").strip().rstrip(".,")
            if doi == ref_doi:
                return {"present": True, "attribution": "matching-source-confirmed", "basis": "Numbered citation maps to an extracted reference with the supplied source DOI."}
    return {"present": True, "attribution": "nearby-unverified", "basis": "A nearby citation was recognized, but attribution to this matching source was not established."}


def _manuscript_view(document: dict, tokens: list[tuple], evidence: list[tuple],
                     scored: set[int], numbers: dict[str, int]) -> list[dict]:
    labels: dict[int, set[int]] = defaultdict(set)
    for match, aligned in evidence:
        for index in aligned:
            labels[index].add(numbers[str(match["source_id"])])
    pages = []
    offset = 0
    for page in document.get("pages") or [{"number": 1, "text": document["text"]}]:
        text = page["text"]
        ranges = []
        for index, sources in sorted(labels.items()):
            start, end = tokens[index][1:3]
            if start < offset + len(text) and end > offset:
                ranges.append({"start": max(0, start - offset), "end": min(len(text), end - offset),
                               "sources": sorted(sources), "excluded": index not in scored})
        pages.append({"number": page.get("number", 1), "text": text, "start": offset, "highlights": ranges})
        offset += len(text) + 2
    return pages


def _quote_portions(aligned: set[int], source_aligned: set[int], quote_regions: list[int],
                   quote_mask: list[bool], exclude_quotes: bool) -> list[dict]:
    groups: list[list[tuple[int, int]]] = []
    for manuscript_word, source_word in zip(sorted(aligned), sorted(source_aligned)):
        if not groups or quote_regions[manuscript_word] != quote_regions[groups[-1][-1][0]]:
            groups.append([])
        groups[-1].append((manuscript_word, source_word))
    portions = []
    for group in groups:
        ma, sa = group[0]
        mb, sb = group[-1][0] + 1, group[-1][1] + 1
        similarity = 2 * len(group) / (mb - ma + sb - sa)
        quoted = quote_mask[ma]
        reasons = []
        if exclude_quotes:
            if quoted:
                reasons.append("recognized-quotation")
            if len(group) < MIN_EXACT_WORDS:
                reasons.append("below-nine-matched-words-after-exclusions")
            elif not quoted and similarity < NEAR_THRESHOLD:
                reasons.append("below-local-alignment-threshold-after-exclusions")
        portions.append({
            "ma": ma, "mb": mb, "sa": sa, "sb": sb, "quoted": quoted,
            "aligned": {pair[0] for pair in group}, "source_aligned": {pair[1] for pair in group},
            "similarity": similarity, "reasons": reasons,
            # With quotation filtering off, splitting here is presentation only:
            # the original qualifying local alignment still supplies the threshold.
            "qualifying_words": len(group) if exclude_quotes else len(aligned),
        })
    return portions


def _ending_credit(left: str, right: str) -> float:
    """Small suffix edits may aid alignment, but never become exact word matches."""
    if min(len(left), len(right)) < 5 or abs(len(left) - len(right)) > 2:
        return 0.0
    prefix = 0
    while prefix < min(len(left), len(right)) and left[prefix] == right[prefix]:
        prefix += 1
    if prefix < 5 or max(len(left), len(right)) - prefix > 2:
        return 0.0
    a, b = left[prefix:], right[prefix:]
    row = list(range(len(b) + 1))
    for i, char in enumerate(a, 1):
        next_row = [i]
        for j, other in enumerate(b, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (char != other)))
        row = next_row
    distance = row[-1]
    credit = 1 - distance / max(len(left), len(right))
    return credit if 0 < distance <= 2 and credit >= ENDING_CREDIT_MINIMUM else 0.0


def _strong_anchors(words: list[str], start: int, end: int) -> dict[tuple, list[int]]:
    anchors: dict[tuple, list[int]] = defaultdict(list)
    for position in range(start, end - ANCHOR_WORDS + 1):
        anchor = tuple(words[position:position + ANCHOR_WORDS])
        if sum(len(word) >= 4 for word in anchor) >= 2:
            anchors[anchor].append(position)
    return anchors


def _flank_alignments(left: list[str], right: list[str]) -> list[dict]:
    """Monotone paths outward from an anchor, within at most eight tokens per side."""
    states: dict[tuple[int, int], dict[tuple[int, int], tuple[float, tuple]]] = {}
    options = []
    for i, word in enumerate(left):
        for j, other in enumerate(right):
            exact = word == other
            credit = 1.0 if exact else _ending_credit(word, other)
            if not credit:
                continue
            current: dict[tuple[int, int], tuple[float, tuple]] = {}
            if max(i, j) <= 6:
                current[(int(exact), int(not exact))] = (credit, ((i, j, exact, credit),))
            for (pi, pj), previous in states.items():
                if not (pi < i and pj < j and max(i - pi - 1, j - pj - 1) <= 6):
                    continue
                for (count, variants), (weight, path) in previous.items():
                    key = (count + int(exact), variants + int(not exact))
                    if key[1] > 1:
                        continue
                    candidate = (weight + credit, path + ((i, j, exact, credit),))
                    if key not in current or candidate[0] > current[key][0]:
                        current[key] = candidate
            states[(i, j)] = current
            # Both outer endpoints must be exact; fuzzy endings only occur inside.
            if exact:
                for (count, variants), (weight, path) in current.items():
                    options.append({"m_span": i + 1, "s_span": j + 1, "count": count,
                                    "variants": variants, "weight": weight, "path": path})
    return options


def _anchored_proposals(mw: list[str], sw: list[str], ms: int, me: int, ss: int, se: int,
                        manuscript_anchors: dict, source_anchors: dict, standard: list,
                        quote_regions: list[int], exclude_quotes: bool,
                        check: Callable[[], None]) -> list[tuple]:
    accepted_pairs = set()
    for proposal in standard:
        accepted_pairs.update(zip(sorted(proposal[6]), sorted(proposal[7])))
    proposals = {}
    for anchor in sorted(manuscript_anchors.keys() & source_anchors.keys()):
        for ma in manuscript_anchors[anchor]:
            for sa in source_anchors[anchor]:
                check()
                anchor_pairs = {(ma + i, sa + i) for i in range(ANCHOR_WORDS)}
                if anchor_pairs <= accepted_pairs:
                    continue
                left = _flank_alignments(mw[max(ms, ma - ANCHOR_FLANK_WORDS):ma][::-1],
                                        sw[max(ss, sa - ANCHOR_FLANK_WORDS):sa][::-1])
                right = _flank_alignments(mw[ma + ANCHOR_WORDS:min(me, ma + ANCHOR_WORDS + ANCHOR_FLANK_WORDS)],
                                         sw[sa + ANCHOR_WORDS:min(se, sa + ANCHOR_WORDS + ANCHOR_FLANK_WORDS)])
                best = None
                for before in left:
                    check()
                    for after in right:
                        count = ANCHOR_WORDS + before["count"] + after["count"]
                        if count < MIN_NEAR_WORDS or before["variants"] + after["variants"] > 1:
                            continue
                        start_m, start_s = ma - before["m_span"], sa - before["s_span"]
                        end_m = ma + ANCHOR_WORDS + after["m_span"]
                        end_s = sa + ANCHOR_WORDS + after["s_span"]
                        if exclude_quotes and quote_regions[start_m] != quote_regions[end_m - 1]:
                            continue
                        similarity = 2 * (ANCHOR_WORDS + before["weight"] + after["weight"]) / (end_m - start_m + end_s - start_s)
                        if similarity < NEAR_THRESHOLD:
                            continue
                        pairs = set(anchor_pairs)
                        variant_credits = []
                        for part, direction in [(before, -1), (after, 1)]:
                            for i, j, exact, credit in part["path"]:
                                mpos = ma - 1 - i if direction == -1 else ma + ANCHOR_WORDS + i
                                spos = sa - 1 - j if direction == -1 else sa + ANCHOR_WORDS + j
                                if exact:
                                    pairs.add((mpos, spos))
                                else:
                                    variant_credits.append({"manuscript_word": mpos, "source_word": spos, "credit": credit})
                        rank = (count, similarity, -(end_m - start_m + end_s - start_s))
                        if best is None or rank > best[0]:
                            best = (rank, start_m, end_m, start_s, end_s, similarity, pairs, variant_credits)
                if best:
                    _, start_m, end_m, start_s, end_s, similarity, pairs, variants = best
                    key = (start_m, end_m, start_s, end_s, tuple(sorted(pairs)))
                    policy = {"method": "anchored-character-ending", "weighted_similarity": similarity,
                              "exact_token_similarity": 2 * len(pairs) / (end_m - start_m + end_s - start_s),
                              "anchor_words": ANCHOR_WORDS, "ending_variants": variants,
                              "note": "Ending credits aid alignment only; nonidentical words are not highlighted or scored."}
                    proposals[key] = ("near-verbatim", start_m, end_m, start_s, end_s, similarity,
                                      {p[0] for p in pairs}, {p[1] for p in pairs}, policy)
    return [proposals[key] for key in sorted(proposals)]


def compare_documents(manuscript: dict, sources: list[dict],
                      cancelled: Callable[[], bool] | None = None, *, exclude_quotes: bool = True,
                      progress: Callable[[str], None] | None = None,
                      load_document: Callable[[dict], dict] | None = None,
                      source_done: Callable[[dict, list[dict]], None] | None = None,
                      load_checkpoint: Callable[[dict], dict | None] | None = None,
                      match_policy: dict | None = None,
                      manuscript_scope: str = "abstract-onward") -> dict:
    """Return lexical evidence and a union-of-matched-word primary overlap score.

    Only aligned equal tokens count (including inside near-verbatim evidence).
    References and (by default) recognized quotations are excluded from scoring.
    Quoted matches remain evidence. Citations and conventional phrasing are eligible.
    Retained evidence bounds the score: truncation produces an explicit warning.
    """
    warnings = list(manuscript.get("warnings", []))

    def check() -> None:
        if cancelled and cancelled():
            raise ComparisonCancelled("Comparison cancelled.")

    check()
    if len(manuscript.get("text", "")) > MAX_DOCUMENT_CHARACTERS:
        raise ValueError("Manuscript exceeds the 1,000,000-character comparison limit.")
    manuscript_text = manuscript.get("text", "")
    join_words = _line_join_words([manuscript_text])
    compounds = _compound_spellings(manuscript_text)
    load_errors = {}
    for source in sources:
        check()
        if not source.get("excluded"):
            try:
                document = load_document(source) if load_document else source.get("document") or {}
            except (OSError, ValueError) as exc:
                load_errors[str(source.get("id"))] = str(exc)
                continue
            text = document.get("text", "")
            if len(text) <= SOURCE_MAX_CHARACTERS:
                join_words.update(_line_join_words([manuscript_text, text]))
                compounds.update(_compound_spellings(text))
    join_words.difference_update(compounds)
    mt = _tokens(manuscript.get("text", ""), join_words, typed=match_policy is not None)
    mw = [token[0] for token in mt]
    ref_mask = _mask(mt, _intervals(manuscript))
    quote_intervals, uncertain_quotes = _quotation_intervals(manuscript["text"])
    quote_mask = _mask(mt, quote_intervals)
    # Manuscript-only analysis scope; comparison sources are never restricted.
    scope = {"requested": manuscript_scope, "applied": "whole-document", "start_offset": None,
             "start_page": None, "heading_text": None, "reason": "Whole manuscript requested."}
    if manuscript_scope == "abstract-onward":
        found = abstract_start(manuscript)
        if found["start_offset"] is None:
            scope["reason"] = "Abstract heading not detected; whole manuscript used."
            warnings.append("Abstract heading not detected; the whole manuscript was analyzed (front matter was not excluded).")
        else:
            scope.update(found, applied="abstract-onward", reason="Analysis starts at the first structural Abstract heading.")
    start_offset = scope["start_offset"] if scope["applied"] == "abstract-onward" else None
    front_mask = [start_offset is not None and token[1] < start_offset for token in mt]
    scope_mask = [reference or front for reference, front in zip(ref_mask, front_mask)]
    if start_offset is not None:
        warnings.append(f"Analysis starts at the Abstract heading on page {scope['start_page']}; {sum(front_mask)} preceding "
                        "front-matter words were excluded from matching and the score denominator.")
    manuscript_regions = _boundary_regions(scope_mask, quote_mask)
    uncertain_mask = _mask(mt, sorted(uncertain_quotes))
    eligible = [not excluded and not (exclude_quotes and quoted) for excluded, quoted in zip(scope_mask, quote_mask)]
    quote_regions = []
    region = 0
    for i, quoted in enumerate(quote_mask):
        if i and quoted != quote_mask[i - 1]:
            region += 1
        quote_regions.append(region)
    token_starts = {token[1]: index for index, token in enumerate(mt)}
    if uncertain_quotes:
        warnings.append("Unpaired or ambiguous quotation delimiters were found. Those uncertain spans remain eligible; review them manually.")
    warnings.append("Paired supported quotation marks, nested quotes and explicit > lines are recognized. Apostrophes are guarded. PDF indentation alone is uncertain and never automatically excluded; this is deterministic recognition, not vendor ML.")
    # Eligible windows drive scoring independently of excluded text. Original
    # bibliography-free windows additionally retain excluded quotation evidence.
    m_windows = sorted(set(_windows(mt, scope_mask, manuscript) +
                           _windows(mt, [not word for word in eligible], manuscript)))
    index: dict[tuple, list[int]] = defaultdict(list)
    anchor_cache = {}
    for window_id, (start, end) in enumerate(m_windows):
        check()
        for shingle in {tuple(mw[i:i + SHINGLE_WORDS]) for i in range(start, end - SHINGLE_WORDS + 1)}:
            index[shingle].append(window_id)
    fingerprint = hashlib.sha256(" ".join(mw).encode()).digest()
    covered: set[int] = set()
    matches: list[dict] = []
    evidence: list[tuple[dict, set[int]]] = []
    seen: set[tuple] = set()
    identical_sources = []
    compared_sources = 0
    truncated = False
    source_warnings = []
    source_coverage = []
    evidence_bytes = 0
    memory_exhausted = False
    for source_number, source in enumerate(sources):
        check()
        source_id = source.get("id", f"source-{source_number + 1}")
        if source.get("excluded"):
            source_coverage.append({"source_id": source_id, "status": "excluded-by-user", "reason": source.get("exclusion_reason") or "Excluded by user."})
            continue
        try:
            document = load_document(source) if load_document else source.get("document")
        except (OSError, ValueError) as exc:
            document = None
            load_errors[str(source_id)] = str(exc)
        if not isinstance(document, dict) or not document.get("text"):
            source_warnings.append(f"Source {source_id}: no extracted document was available for comparison.")
            source_coverage.append({"source_id": source_id, "status": "unavailable", "reason": load_errors.get(str(source_id), "No readable extracted text.")})
            if source_done:
                source_done(source_coverage[-1], [])
            continue
        if len(document["text"]) > SOURCE_MAX_CHARACTERS:
            reason = f"Exceeds the {SOURCE_MAX_CHARACTERS:,}-character source comparison limit; not compared."
            source_warnings.append(f"Source {source_id}: {reason}")
            source_coverage.append({"source_id": source_id, "status": "skipped-size-limit", "reason": reason})
            continue
        if memory_exhausted:
            truncated = True
            source_coverage.append({"source_id": source_id, "status": "skipped-memory-limit",
                                    "reason": "The declared 64 MiB evidence-memory safety limit was reached before this source."})
            continue
        st = _tokens(document["text"], join_words, typed=match_policy is not None)
        sw = [token[0] for token in st]
        if hashlib.sha256(" ".join(sw).encode()).digest() == fingerprint:
            identical_sources.append(source_id)
            source_coverage.append({"source_id": source_id, "status": "excluded-identical"})
            continue
        compared_sources += 1
        cached = load_checkpoint(source) if load_checkpoint else None
        if cached and cached.get("coverage", {}).get("status") == "compared":
            restored = cached["matches"]
            for match in restored:
                aligned = {token_starts[match["manuscript"]["start"] + span[0]] for span in match["manuscript"]["highlights"]}
                evidence_bytes += 2048 + 4 * (len(match["manuscript"]["text"]) + len(match["source"]["text"])) + 128 * len(aligned)
                matches.append(match)
                evidence.append((match, aligned))
                covered.update(match["scored_word_positions"])
            row = {**cached["coverage"], "resumed_from_checkpoint": True}
            source_coverage.append(row)
            if source_done:
                source_done(row, restored)
            continue
        coverage = {"source_id": source_id, "status": "compared", "retained_matches": 0,
                    "limits_reached": [], "candidate_windows_omitted": 0,
                    "source_windows_visited": 0, "comparisons_performed": 0}
        source_coverage.append(coverage)
        initial_matches = len(matches)
        source_started = time.monotonic()
        timed_out = False
        source_excluded = _mask(st, _intervals(document))
        comparisons = 0
        source_windows = _windows(st, source_excluded, document)
        coverage["source_windows_total"] = len(source_windows)
        for ss, se in source_windows:
            check()
            coverage["source_windows_visited"] += 1
            if progress and coverage["source_windows_visited"] % 10 == 1:
                progress(f"Comparing source {source_number + 1}/{len(sources)}: window {coverage['source_windows_visited']}/{len(source_windows)}.")
            candidates: Counter = Counter()
            source_anchors = _strong_anchors(sw, ss, se)
            for shingle in {tuple(sw[i:i + SHINGLE_WORDS]) for i in range(ss, se - SHINGLE_WORDS + 1)}:
                candidates.update(index.get(shingle, ()))
            for window_id, _ in sorted(candidates.items(), key=lambda item: (-item[1], item[0])):
                check()
                if time.monotonic() - source_started >= MAX_SOURCE_SECONDS:
                    timed_out = True
                    break
                comparisons += 1
                coverage["comparisons_performed"] = comparisons
                ms, me = m_windows[window_id]
                blocks = [block for block in SequenceMatcher(None, mw[ms:me], sw[ss:se], autojunk=False).get_matching_blocks()
                          if block.size]
                # Join aligned blocks with small edits; large unrelated gaps remain separate evidence.
                groups: list[list] = []
                for block in blocks:
                    previous = groups[-1][-1] if groups else None
                    if previous and max(block.a - previous.a - previous.size,
                                                                      block.b - previous.b - previous.size) <= 6:
                        groups[-1].append(block)
                    else:
                        groups.append([block])
                proposed = []
                for group in groups:
                    first, last = group[0], group[-1]
                    ma, mb = ms + first.a, ms + last.a + last.size
                    sa, sb = ss + first.b, ss + last.b + last.size
                    equal = sum(block.size for block in group)
                    similarity = 2 * equal / ((mb - ma) + (sb - sa))
                    if len(group) > 1 and equal >= MIN_NEAR_WORDS and similarity >= NEAR_THRESHOLD:
                        proposed.append(("near-verbatim", ma, mb, sa, sb, similarity,
                                         {ms + block.a + j for block in group for j in range(block.size)},
                                         {ss + block.b + j for block in group for j in range(block.size)}))
                    else:
                        for block in group:
                            if block.size >= MIN_EXACT_WORDS:
                                proposed.append(("exact", ms + block.a, ms + block.a + block.size,
                                                 ss + block.b, ss + block.b + block.size, 1.0,
                                                 set(range(ms + block.a, ms + block.a + block.size)),
                                                 set(range(ss + block.b, ss + block.b + block.size))))
                portions = []
                if window_id not in anchor_cache:
                    anchor_cache[window_id] = _strong_anchors(mw, ms, me)
                def check_anchor_budget():
                    check()
                    if time.monotonic() - source_started >= MAX_SOURCE_SECONDS:
                        raise _SourceTimeLimit
                try:
                    if match_policy:
                        # Extend the retrieval window on both sides; stop extension
                        # at excluded bibliography/quotation regions, not page edges.
                        margin = int(match_policy["maximum_gap"]) * 2
                        ems, eme = max(0, ms - margin), min(len(mw), me + margin)
                        ess, ese = max(0, ss - margin), min(len(sw), se + margin)
                        for pos in range(ems, ms):
                            if scope_mask[pos] or (exclude_quotes and quote_regions[pos] != quote_regions[ms]):
                                ems = pos + 1
                        for pos in range(ms, eme):
                            if scope_mask[pos] or (exclude_quotes and quote_regions[pos] != quote_regions[ms]):
                                eme = pos
                                break
                        for pos in range(ess, ss):
                            if source_excluded[pos]:
                                ess = pos + 1
                        for pos in range(ss, ese):
                            if source_excluded[pos]:
                                ese = pos
                                break
                        regions = quote_regions if exclude_quotes else [0] * len(mw)
                        anchored = ordered_instances(mw, sw, ems, eme, ess, ese, match_policy, regions, check_anchor_budget)
                    else:
                        anchored = _anchored_proposals(
                            mw, sw, ms, me, ss, se, anchor_cache[window_id], source_anchors,
                            proposed, quote_regions, exclude_quotes, check_anchor_budget,
                        )
                except _SourceTimeLimit:
                    timed_out = True
                    break
                annotated = anchored if match_policy else [(*proposal, {"method": "standard-exact-token"}) for proposal in proposed] + anchored
                for kind, ma, mb, sa, sb, similarity, aligned, source_aligned, policy in annotated:
                    for portion in _quote_portions(aligned, source_aligned, quote_regions, quote_mask, exclude_quotes):
                        if policy["method"] in {"anchored-character-ending", "ordered-local-instance"}:
                            # The anchored candidate was checked in one quotation
                            # region, or quote filtering is off (display split only).
                            portion["reasons"] = [reason for reason in portion["reasons"]
                                                  if reason != "below-local-alignment-threshold-after-exclusions"]
                            if len(portion["aligned"]) == len(aligned):
                                portion["similarity"] = similarity
                        portion["parent_matched_words"] = len(aligned)
                        portion["parent_similarity"] = similarity
                        portion["alignment_policy"] = policy
                        portions.append(portion)
                for portion in portions:
                    ma, mb, sa, sb = (portion[field] for field in ("ma", "mb", "sa", "sb"))
                    aligned, source_aligned = portion["aligned"], portion["source_aligned"]
                    similarity = portion["similarity"]
                    kind = "exact" if len(aligned) == mb - ma == sb - sa else "near-verbatim"
                    scored = {word for word in aligned if eligible[word]} if not portion["reasons"] else set()
                    key = (str(source_id), ma, mb, sa, sb, tuple(sorted(aligned)), tuple(sorted(source_aligned)), bool(scored))
                    if key in seen:
                        continue
                    passage = _passage(manuscript, mt, ma, mb, aligned)
                    flags = []
                    quoted_words = sum(quote_mask[word] for word in aligned)
                    if quoted_words:
                        flags.append("quotation-context")
                    context = passage["text"]
                    citation = _citation_context(context, manuscript, source)
                    if citation["present"]:
                        flags.append("citation-context")
                    if _BOILERPLATE.search(passage["text"]):
                        flags.append("possible-boilerplate")
                    classification = (
                        "Cited quotation" if citation["present"] and quoted_words else
                        "Quotation—citation may be missing" if quoted_words else
                        "Cited wording—quotation may be needed" if citation["present"] else "Unattributed text overlap"
                    )
                    match = {"source_id": source_id, "kind": kind, "manuscript": passage,
                             "source": _passage(document, st, sa, sb, source_aligned),
                             "similarity": round(similarity, 4), "flags": flags,
                             "classification": classification, "citation": citation,
                             "quotation": {"status": "recognized" if quoted_words else "uncertain" if any(uncertain_mask[word] for word in aligned) else "not-detected",
                                           "matched_quoted_words": quoted_words},
                             "matched_words": len(aligned), "included_words": len(scored),
                             "excluded_words": len(aligned) - len(scored),
                             "exclusion_reasons": portion["reasons"],
                             "qualifying_alignment_words": portion["qualifying_words"],
                             "parent_alignment_matched_words": portion["parent_matched_words"],
                             "parent_alignment_similarity": portion["parent_similarity"],
                             "alignment_policy": portion["alignment_policy"],
                             "aligned_pairs": [list(pair) for pair in zip(sorted(aligned), sorted(source_aligned))],
                             "scored_word_positions": sorted(scored)}
                    match["excluded_from_score"] = match["included_words"] == 0
                    if policy["method"] == "ordered-local-instance" and scored:
                        for mpos, spos, length in policy.get("partial_numeric_pairs", []):
                            if ma <= mpos < mb and sa <= spos < sb and eligible[mpos]:
                                match["manuscript"]["highlights"].append([mt[mpos][1] - passage["start"], mt[mpos][1] - passage["start"] + length])
                                match["source"]["highlights"].append([st[spos][1] - match["source"]["start"], st[spos][1] - match["source"]["start"] + length])
                        match["manuscript"]["highlights"].sort()
                        match["source"]["highlights"].sort()
                    estimate = 2048 + 4 * (len(match["manuscript"]["text"]) + len(match["source"]["text"])) + 128 * len(aligned)
                    if evidence_bytes + estimate > MAX_EVIDENCE_BYTES:
                        memory_exhausted = True
                        break
                    evidence_bytes += estimate
                    seen.add(key)
                    matches.append(match)
                    evidence.append((match, aligned))
                    covered.update(scored)
                if memory_exhausted:
                    break
            if timed_out or memory_exhausted:
                truncated = True
                coverage["status"] = "compared-with-limits"
                coverage["limits_reached"].append("source-time-limit" if timed_out else "evidence-memory-limit")
                coverage["reason"] = (
                    f"Stopped after the declared {MAX_SOURCE_SECONDS}-second per-source time limit."
                    if timed_out else "Stopped at the declared 64 MiB estimated evidence-memory safety limit."
                )
                break

        def check_consolidation_budget():
            check()
            if time.monotonic() - source_started >= MAX_SOURCE_SECONDS:
                raise _SourceTimeLimit

        source_quotes = _mask(st, _quotation_intervals(document["text"])[0])
        try:
            merged = merge_instances(
                matches[initial_matches:], manuscript_regions=manuscript_regions,
                source_regions=_boundary_regions(source_excluded, source_quotes),
                check=check_consolidation_budget,
            )
        except _SourceTimeLimit:
            truncated = True
            coverage["status"] = "compared-with-limits"
            coverage["consolidation_complete"] = False
            if "source-time-limit" not in coverage["limits_reached"]:
                coverage["limits_reached"].append("source-time-limit")
            coverage["reason"] = (
                f"The {MAX_SOURCE_SECONDS}-second per-source budget ended before evidence consolidation completed. "
                "Original retained candidates remain available; some duplicate evidence may remain."
            )
        else:
            matches[initial_matches:] = merged
            evidence[initial_matches:] = [(match, {pair[0] for pair in match["aligned_pairs"]}) for match in merged]
            coverage["consolidation_complete"] = True
        coverage["retained_matches"] = len(matches) - initial_matches
        if source_done:
            source_done(coverage, matches[initial_matches:])
    per_word_sources: dict[int, set[str]] = defaultdict(set)
    for match, aligned in evidence:
        for word in aligned:
            per_word_sources[word].add(str(match["source_id"]))
    for match, aligned in evidence:
        if sum(len(per_word_sources[word]) >= 3 for word in aligned) >= MIN_EXACT_WORDS and "cross-source-repetition" not in match["flags"]:
            match["flags"].append("cross-source-repetition")
    if identical_sources:
        warnings.append("Excluded normalized-identical manuscript copies: " + ", ".join(map(str, identical_sources)))
    if truncated:
        warnings.append(
            "A declared resource safety limit was reached; exact per-source reasons and traversal counts are recorded. "
            "Retained evidence and score are a lower bound, not a completed comparison."
        )
    warnings.extend(source_warnings)
    denominator = sum(eligible)
    front_words = sum(front_mask)
    score_denominator = len(mt) - front_words
    basis, policy_version = (("abstract-onward-word-units", "abstract-onward-v1") if start_offset is not None
                             else ("all-submitted-word-units", "total-document-v1"))
    source_words: dict[str, set[int]] = defaultdict(set)
    for match, aligned in evidence:
        source_words[str(match["source_id"])].update(match["scored_word_positions"])
    for row in source_coverage:
        if row["status"] in {"compared", "compared-with-limits"}:
            count = len(source_words[str(row["source_id"])])
            row.update(
                overlapping_words=count, eligible_words=denominator, score_denominator_words=score_denominator,
                overlap_percent=round(100 * count / score_denominator, 2) if score_denominator else 0.0,
            )
    if not denominator:
        warnings.append("No eligible manuscript words remain after exclusions. A zero score on a nonempty total-document denominator reflects filters, not an originality finding.")
    source_numbers = {str(source.get("id", f"source-{i + 1}")): source.get("source_number", i + 1) for i, source in enumerate(sources)}
    for match, aligned in evidence:
        match["source_number"] = source_numbers[str(match["source_id"])]
        alternatives = set().union(*(per_word_sources[word] for word in aligned))
        match["alternative_source_ids"] = sorted(alternatives - {str(match["source_id"])})
    result = {
        "algorithm_version": "experimental-" + match_policy["id"] if match_policy else ALGORITHM_VERSION,
        "comparison_model": "experimental-ordered" if match_policy else "validated-lexical",
        "quality_notice": "Experimental: increased reference recall did not establish precision; not vendor-equivalent."
        if match_policy else "Local lexical rules; no proprietary-score equivalence claim.",
        "settings": {"minimum_matched_words": 9, "exclude_bibliography": True, "exclude_quotes": exclude_quotes,
                     "configured_profile": "reference-report-less-than-nine",
                     "small_match_unit": "Buna local alignment instance, not each displayed highlight rectangle",
                     "small_source_aggregate_filter": "unsupported",
                     "exclude_cited_text": False,
                     "quotation_policy": "documented-forms-deterministic-v1",
                     "score_basis": basis,
                     "score_policy_version": policy_version,
                     "manuscript_scope": scope,
                     "vendor_post_filter_denominator": "unresolved; total-document basis is a declared interpretation",
                     "near_similarity": NEAR_THRESHOLD, "maximum_alignment_gap_words": 6,
                     "filter_order": "exclude-regions-then-local-nine-word-minimum",
                     "evidence_consolidation": "compatible-paired-union-v1",
                     "mixed_quote_presentation": "separate-portions; with quotation filtering off, parent alignment supplies minimum",
                     "anchored_alternative": {"anchor_words": ANCHOR_WORDS, "flank_words": ANCHOR_FLANK_WORDS,
                                              "minimum_exact_words": MIN_NEAR_WORDS, "minimum_weighted_similarity": NEAR_THRESHOLD,
                                              "maximum_ending_variants": 1, "minimum_ending_credit": ENDING_CREDIT_MINIMUM},
                     "source_time_limit_seconds": MAX_SOURCE_SECONDS, "estimated_evidence_memory_limit_mib": 64,
                     "source_capacity_profile": "source-v1", "source_character_limit": SOURCE_MAX_CHARACTERS,
                     "candidate_limit": None, "comparison_count_limit": None, "finding_display_page_size": 10},
        "metrics": {"total_words": len(mt), "eligible_words": denominator,
                    "score_denominator_words": score_denominator,
                    "analyzed_words": score_denominator, "front_matter_words": front_words,
                    "score_basis": basis,
                    "score_policy_version": policy_version,
                    "overlapping_words": len(covered),
                    "overlap_percent": round(100 * len(covered) / score_denominator, 2) if score_denominator else 0.0,
                    "eligible_body_overlap_percent": round(100 * len(covered) / denominator, 2) if denominator else None,
                    "bibliography_words": sum(ref_mask),
                    "quotation_words": sum(q and not r for q, r in zip(quote_mask, scope_mask)),
                    "excluded_quotation_words": sum(q and not r for q, r in zip(quote_mask, scope_mask)) if exclude_quotes else 0,
                    "unscorable": score_denominator == 0,
                    "all_text_excluded": denominator == 0 and score_denominator > 0,
                    "quotation_overlap_words": sum(quote_mask[index] for index in covered),
                    "sources_supplied": len(sources), "sources_compared": compared_sources,
                    "identical_sources_excluded": len(identical_sources), "truncated": truncated},
        "matches": matches, "warnings": warnings, "source_coverage": source_coverage,
        "excluded_source_ids": identical_sources,
        "manuscript_pages": _manuscript_view(manuscript, mt, evidence, covered, source_numbers),
        "methodology": {
            "candidate_policy": match_policy,
            "name": "Bounded lexical passage comparison",
            "normalization": "Unicode NFKC, case-folded word tokens; punctuation and whitespace ignored. "
                             "Join soft-hyphen fragments. Join a hyphen-plus-single-linebreak only with alphabetic "
                             "preceding text, lowercase continuation, an unbroken joined spelling found in the compared texts, "
                             "and no unwrapped hyphenated spelling found there. Otherwise keep separate tokens. "
                             "Original text and offsets are unchanged. Ambiguous hyphens may still be imperfect. No semantic analysis.",
            "offsets": "Zero-based, end-exclusive Unicode code-point character offsets in original extracted text. "
                       "Passage highlights are relative [start, end] ranges for each aligned word, not edited words.",
            "thresholds": {"shingle_words": SHINGLE_WORDS, "minimum_exact_words": MIN_EXACT_WORDS,
                           "minimum_near_aligned_words": MIN_NEAR_WORDS, "near_similarity": NEAR_THRESHOLD},
            "algorithm": "Index 2-word seeds in manuscript windows of at most 120 words, overlapping by "
                         "24 words, crossing sentence/segment/page boundaries but not bibliography. No boilerplate seeds are suppressed. "
                         "Process ALL candidate manuscript windows per source window, ordered by shared seeds, then use "
                         "SequenceMatcher aligned words. Join matching runs separated by at most 6 words. "
                         "The six-word gap bound applies separately on both sides. Apply exclusions FIRST and split at every excluded region. "
                         "Require 9 actual eligible aligned words in EACH remaining local group "
                         "and near similarity >=0.82 (twice aligned words divided by both local span word counts). "
                         "Groups failing that threshold score only individual exact runs of at least 9 words. "
                         "Quoted portions and short survivors from a previously qualifying original alignment remain inspectable with exclusion reasons. "
                         "When quotation exclusion is disabled, the normal original local alignment supplies the nine-word threshold; mixed quotation portions "
                         "are split only for classification and their qualifying parent count is recorded. "
                         "Scattered words are never summed across windows. Sentence context extends at most600 characters per side. "
                         "No candidate-count, comparison-count or finding-count cap stops scoring. Display pagination is independent. "
                         "A 120-second per-source time limit and 64 MiB estimated evidence-memory safety budget can stop pathological "
                         "runs with explicit per-source reasons and lower-bound results; all candidates otherwise finish.",
            "anchored_alternative": (
                "The standard >=0.82 exact-token path is unchanged. A second ordered path requires an exact six-word anchor "
                "containing at least two tokens of four or more characters, exact support on BOTH sides, and at least nine "
                "actually equal eligible words overall. Search is limited to eight tokens on each side of the anchor; "
                "gaps between aligned tokens remain <=6 per side. At most one nonidentical word-ending pair may aid alignment: "
                "both words >=5 characters, identical prefix >=5 characters, at most two trailing characters each, suffix "
                "edit distance <=2 and character credit (1-distance/longer length) >=0.85. "
                "Weighted similarity = 2*(equal words + ending credit)/(both span word counts) must still be >=0.82. "
                "Only equal tokens count toward the nine-word minimum, overlap numerator and word highlights. "
                "The outer endpoints are exact, reordered fragments are not pooled, and enabled quotation/bibliography exclusions "
                "are never bridged. One best path per anchor is selected by exact count, weighted similarity, then shorter span; "
                "identical evidence is deduplicated while distinct source locations remain."
            ),
            "denominator": "By default, manuscript word units from the first structural Abstract heading onward (abstract-onward-v1); front matter before it "
                           "(title, authors, affiliations, funding, cover pages) is excluded from matching and from the denominator and counted separately. "
                           "If no Abstract heading is recognized, all submitted word units are used with a visible warning (total-document-v1). "
                           "Bibliography and quotation exclusions remove included matches, not denominator units. Comparison sources are not scoped. "
                           "Proprietary post-filter arithmetic is not established. Eligible/excluded counts remain separate.",
            "numerator": "Union of eligible manuscript token positions aligned to identical normalized source words "
                         "in retained evidence. Overlapping sources and evidence never double-count.",
            "evidence_consolidation": "Both models canonicalize compatible already-qualified candidates before checkpoints and report counts. "
                                      "Candidates must share an exact word pair and retain one-to-one monotone mappings, matching citation/category "
                                      "metadata and quotation/bibliography regions in both documents. No gap words are invented and no matching "
                                      "threshold is reapplied to the union. Distinct source locations and incompatible alignments remain separate. "
                                      "Original candidate policies and qualification data remain available. The per-source time budget includes "
                                      "consolidation; incomplete consolidation retains original evidence with explicit partial status. "
                                      "The memory guard conservatively includes candidates before consolidation.",
            "exclusions": "Recognized bibliography is excluded from candidate search and scoring in both documents. "
                          "Normalized-identical manuscripts are excluded. Supported paired quotation marks (including single, double, guillemets, German and East Asian forms), nested quotes and "
                          "> blockquote lines are excluded from scoring by default but their evidence is retained. "
                          "Uncertain quotation boundaries, inline citations and conventional phrases do not suppress scores. "
                          "Explicit user source exclusions never depend on guessed preprint status.",
            "limitations": "Lexical similarity is not a plagiarism finding, authorship judgment, semantic similarity "
                           "or legal conclusion. Short matches, translations, paraphrases, punctuation-only changes, "
                           "unrecognized references and quotations, extraction errors, and window boundaries can "
                           "affect results. Citation/boilerplate/repetition flags are heuristics, not attribution "
                           "verification. Uncertain quotation warnings are localized to their paragraph (bounded to 10,000 characters each side), "
                           "or indentation span; intended missing boundaries cannot be reconstructed reliably. "
                           "Citation-source confirmation requires a supplied DOI in the surrounding text or a numbered "
                           "citation mapped to an extracted reference with that DOI; otherwise nearby citations are unverified. "
                           "Coverage is limited to uploaded, successfully extracted sources. A zero score is not clearance. "
                           "These documented rules do not reproduce any proprietary similarity score.",
        },
    }
    if match_policy:
        result["settings"] = {
            **result["settings"], "ordered_instance_policy": dict(match_policy),
            "near_similarity": match_policy["minimum_density"],
            "maximum_alignment_gap_words": match_policy["maximum_gap"],
            "anchored_alternative": None, "experimental": True,
            "score_basis": basis,
        }
        result["methodology"] = {
            "name": "Experimental ordered local match instances",
            "candidate_policy": dict(match_policy),
            "normalization": result["methodology"]["normalization"] +
                " Decimal literals are single typed units; optional numeric prefix evidence marks only equal characters, never substitutes arbitrary values.",
            "offsets": result["methodology"]["offsets"],
            "algorithm": "Retrieve two-word seeds, extend candidate contexts on both sides by twice maximum_gap without crossing exclusions; "
                         "enumerate ordered exact-block subchains. Require minimum_equal_words, declared density, and minimum lexical "
                         "characters for nonexact chains. Keep maximal compatible instances and merge overlap-compatible source-local "
                         "paths across retrieval windows. No six-word-anchor or one-inflection special path. Existing timeout/memory budgets "
                         "remain explicit; this is an experiment, not an exhaustive proprietary-algorithm reconstruction.",
            "denominator": "Submitted typed word units within the manuscript scope (Abstract onward when recognized), including bibliography/quotation regions. Eligible units and filtered-body ratio are retained in the ledger only.",
            "numerator": "Union of included actually equal word positions. Partial numeric prefix evidence, when enabled, is a separate character layer and never whole-word credit.",
            "exclusions": result["methodology"]["exclusions"],
            "limitations": "Provisional same-manuscript benchmark only; additional predictions are unadjudicated, not established true or false positives. "
                          "Higher recall does not imply improved precision. Filter ordering and denominator are explicit experimental policy, "
                          "not verified vendor semantics. All character evidence links to original text/pages.",
        }
    return result
