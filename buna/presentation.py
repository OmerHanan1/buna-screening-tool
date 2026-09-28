"""Lossless evidence grouping and conservative best-source display ranking."""
from collections import defaultdict

TYPE_PRIORITY = {"internet": 0, "publication": 1, "submitted-work": 2}


def evidence_presentation(report: dict) -> dict:
    matches = report.get("matches", [])
    papers = {str(p["id"]): p for p in report.get("papers", [])}
    words = defaultdict(set)
    evidence_words = defaultdict(set)
    groups = {}
    for index, match in enumerate(matches):
        passage = match.get("manuscript", {})
        # Absolute highlighted ranges avoid grouping different occurrences merely
        # because their displayed sentence text happens to be identical.
        spans = tuple(sorted((passage.get("start", 0) + a, passage.get("start", 0) + b)
                             for a, b in passage.get("highlights", [])))
        if not spans:
            spans = ((passage.get("match_start", passage.get("start", 0)),
                      passage.get("match_end", passage.get("end", 0))),)
        key = (spans, bool(match.get("excluded_from_score")))
        groups.setdefault(key, []).append(index)
        source_id = str(match["source_id"])
        evidence_words[source_id].update(spans)
        if "scored_word_positions" in match:
            words[source_id].update(match["scored_word_positions"])
        elif not match.get("excluded_from_score"):
            words[source_id].update(spans)

    def stable(source_id):
        paper = papers.get(str(source_id), {})
        return (paper.get("source_number", 10**9), str(source_id))

    output = []
    source_blocks = defaultdict(int)
    for group_number, indices in enumerate(groups.values(), 1):
        source_ids = {str(matches[i]["source_id"]) for i in indices}
        by_count = defaultdict(list)
        for sid in source_ids:
            by_count[len(words[sid])].append(sid)
        ranked = []
        for count in sorted(by_count, reverse=True):
            tied = by_count[count]
            known = all(papers.get(sid, {}).get("source_type") in TYPE_PRIORITY for sid in tied)
            ranked.extend(sorted(tied, key=lambda sid: (
                TYPE_PRIORITY[papers[sid]["source_type"]] if known else 0, stable(sid))))
        rank = {sid: i for i, sid in enumerate(ranked)}
        ordered = sorted(indices, key=lambda i: (rank[str(matches[i]["source_id"])],
                         matches[i].get("source", {}).get("match_start", matches[i].get("source", {}).get("start", 0)), i))
        output.append({"id": group_number, "match_indices": ordered, "best_match_index": ordered[0],
                       "source_ids": ranked, "alternative_locations": len(ordered) - 1})
        for sid in source_ids:
            source_blocks[sid] += 1
    return {
        "groups": output,
        "source_cards": [{"source_id": sid, "matching_word_spans": len(words[sid]),
                          "all_evidence_word_spans": len(evidence_words[sid]),
                          "target_blocks": source_blocks[sid], "source_type": papers.get(sid, {}).get("source_type", "unknown")}
                         for sid in sorted(papers, key=stable)],
        "ranking_policy": "Most unique included matched manuscript words first; for identical-word ties use Internet, Publications, Submitted Works only when all tied sources have explicit types; otherwise stable source order. Excluded evidence does not improve ranking. Manual uploads are unknown, never inferred from DOI or filename.",
        "scope": "Presentation only. Every source/location and original match remains available; scoring and benchmark evidence are unchanged.",
    }


def manuscript_reader(report: dict, fallback_pages: list[dict] | None = None) -> dict:
    """Build page-preserving display fragments from saved offsets, never re-tokenize."""
    matches = report.get("matches", [])
    presentation = evidence_presentation(report)
    papers = report.get("papers", [])
    numbers = {str(p["id"]): p.get("source_number", i + 1) for i, p in enumerate(papers)}
    outcomes = {str(row["source_id"]): row for row in report.get("source_coverage", [])}
    sources = []
    for paper in papers:
        sid = str(paper["id"])
        outcome = outcomes.get(sid, {})
        status = outcome.get("status") or paper.get("comparison_status") or (
            "compared-with-limits" if paper.get("partial_comparison") else paper.get("status", "unknown"))
        reason = paper.get("error") or paper.get("reason") or paper.get("exclusion_reason") or outcome.get("reason") or {
            "excluded-identical": "Identical manuscript copy; not compared.",
            "skipped-evidence-limit": "The evidence limit was reached before this source was examined.",
            "skipped-size-limit": "Source exceeded the comparison size limit.",
            "skipped-memory-limit": "Memory safety limit reached before this source was examined.",
            "compared-with-limits": "Only a partial comparison was completed; see saved limit details.",
        }.get(status, "")
        sources.append({
            "id": sid, "number": numbers[sid], "title": paper.get("filename") or paper.get("title") or "Untitled source",
            "status": status, "reason": reason,
            "overlap_percent": outcome.get("overlap_percent"),
            "overlapping_words": outcome.get("overlapping_words"),
            "match_indices": [i for i, match in enumerate(matches) if str(match["source_id"]) == sid],
        })
    intervals = []
    for index, match in enumerate(matches):
        passage = match.get("manuscript", {})
        for a, b in passage.get("highlights", []):
            if isinstance(a, int) and isinstance(b, int) and 0 <= a < b <= len(passage.get("text", "")):
                intervals.append((passage.get("start", 0) + a, passage.get("start", 0) + b, index))
    pages, offset = [], 0
    for original in report.get("manuscript_pages") or fallback_pages or []:
        text = original.get("text", "")
        start = original.get("start", offset)
        events = defaultdict(lambda: {"add": set(), "remove": set()})
        events[0]; events[len(text)]
        for a, b, index in intervals:
            left, right = max(start, a) - start, min(start + len(text), b) - start
            if left < right:
                events[left]["add"].add(index)
                events[right]["remove"].add(index)
        active, fragments = set(), []
        points = sorted(events)
        for i, point in enumerate(points[:-1]):
            active.difference_update(events[point]["remove"])
            active.update(events[point]["add"])
            end = points[i + 1]
            indices = sorted(active)
            included = [j for j in indices if not matches[j].get("excluded_from_score")]
            excluded = [j for j in indices if matches[j].get("excluded_from_score")]
            fragment = {"text": text[point:end], "start": point, "end": end,
                        "match_indices": included, "excluded_indices": excluded}
            # Join word-level highlights across whitespace only when they reference
            # the same evidence; every original character is preserved exactly once.
            if len(fragments) >= 2 and not fragments[-1]["match_indices"] and not fragments[-1]["excluded_indices"] \
                    and fragments[-1]["text"].isspace() and "\n\n" not in fragments[-1]["text"] \
                    and (included or excluded) and fragments[-2]["match_indices"] == included \
                    and fragments[-2]["excluded_indices"] == excluded:
                gap = fragments.pop()
                fragments[-1]["text"] += gap["text"] + fragment["text"]
                fragments[-1]["end"] = end
            else:
                fragments.append(fragment)
        pages.append({"number": original.get("number", len(pages) + 1), "start": start, "text": text, "fragments": fragments})
        offset = start + len(text) + 2
    ordered_groups = sorted(presentation["groups"], key=lambda group:
        matches[group["best_match_index"]].get("manuscript", {}).get("match_start", 0))
    return {
        "pages": pages, "sources": sources, "groups": ordered_groups,
        "fidelity": "Page-preserving extracted text, not the original PDF layout. Saved word highlights and offsets are unchanged.",
        "source_number_semantics": "Numbers identify source files, not passage numbers.",
        "legacy_pages_unavailable": not pages,
    }
