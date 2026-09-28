"""Generic ordered match instances; no document-specific words or inflection rules."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Callable


NUMERIC = re.compile(r"^\d+(?:[.,]\d+)*$")
DEFAULT_POLICY = {
    "id": "ordered-chain-strict",
    "maximum_gap": 12,
    "minimum_density": .65,
    "minimum_equal_words": 9,
    "minimum_lexical_characters": 30,
    "numeric_partial_credit": False,
}


def _candidate_provenance(match: dict) -> list[dict]:
    policy = match.get("alignment_policy", {})
    if policy.get("method") == "canonical-paired-union":
        return policy["constituents"]
    pairs = match["aligned_pairs"]
    return [{
        "manuscript_words": [pairs[0][0], pairs[-1][0] + 1],
        "source_words": [pairs[0][1], pairs[-1][1] + 1],
        **{key: match.get(key) for key in (
            "matched_words", "qualifying_alignment_words",
            "parent_alignment_matched_words", "parent_alignment_similarity",
        )},
        "alignment_policy": policy,
    }]


def _alignment_constraints(match: dict) -> set[tuple[int, int]]:
    pairs = {tuple(pair) for pair in match["aligned_pairs"]}
    for candidate in _candidate_provenance(match):
        ma, mb = candidate["manuscript_words"]
        sa, sb = candidate["source_words"]
        policy = candidate["alignment_policy"]
        pairs.update((m, s) for m, s, _ in policy.get("partial_numeric_pairs", [])
                     if ma <= m < mb and sa <= s < sb)
        pairs.update((variant["manuscript_word"], variant["source_word"])
                     for variant in policy.get("ending_variants", [])
                     if ma <= variant["manuscript_word"] < mb and sa <= variant["source_word"] < sb)
    return pairs


def _merge_passages(left: dict, right: dict) -> dict:
    first, last = sorted((left, right), key=lambda p: (p["start"], p["end"]))
    start, end = first["start"], max(first["end"], last["end"])
    text = first["text"] + last["text"][max(0, first["end"] - last["start"]):]
    spans = sorted({(p["start"] + a - start, p["start"] + b - start)
                    for p in (left, right) for a, b in p["highlights"]})
    highlights = []
    for a, b in spans:
        if highlights and a < highlights[-1][1]:
            highlights[-1][1] = max(b, highlights[-1][1])
        else:
            highlights.append([a, b])
    location = min((left, right), key=lambda p: (p["match_start"], p["start"]))
    return {
        **location, "start": start, "end": end, "text": text,
        "match_start": min(left["match_start"], right["match_start"]),
        "match_end": max(left["match_end"], right["match_end"]),
        "highlights": highlights,
        "pages": sorted(set(left.get("pages", [])) | set(right.get("pages", []))),
    }


def _compatible_paths(left: dict, right: dict, left_pairs: set[tuple],
                      right_pairs: set[tuple], regions: tuple[list[int] | None, ...]) -> bool:
    for key in ("source_id", "classification", "citation", "excluded_from_score"):
        if left.get(key) != right.get(key):
            return False
    if set(left.get("exclusion_reasons", [])) != set(right.get("exclusion_reasons", [])):
        return False
    if left.get("quotation", {}).get("status") != right.get("quotation", {}).get("status"):
        return False
    for side, boundary in enumerate(regions):
        if boundary is not None:
            indices = [pair[side] for pair in left_pairs | right_pairs]
            if boundary[min(indices)] != boundary[max(indices)]:
                return False
    shared_words = {m for m, _ in left_pairs} & {m for m, _ in right_pairs}
    if (set(left["scored_word_positions"]) ^ set(right["scored_word_positions"])) & shared_words:
        return False
    constraints = sorted(_alignment_constraints(left) | _alignment_constraints(right))
    if any(a >= b or s >= t for (a, s), (b, t) in zip(constraints, constraints[1:])):
        return False
    for side in ("manuscript", "source"):
        a, b = left[side], right[side]
        start, end = max(a["start"], b["start"]), min(a["end"], b["end"])
        if start >= end or a["text"][start - a["start"]:end - a["start"]] != b["text"][start - b["start"]:end - b["start"]]:
            return False
    return True


def _merge_paths(left: dict, right: dict, pairs: set[tuple]) -> dict:
    ordered = sorted(pairs)
    span = ordered[-1][0] - ordered[0][0] + 1 + ordered[-1][1] - ordered[0][1] + 1
    scored = sorted(set(left["scored_word_positions"]) | set(right["scored_word_positions"]))
    provenance = _candidate_provenance(left) + _candidate_provenance(right)
    provenance = [json.loads(value) for value in sorted({json.dumps(p, sort_keys=True) for p in provenance})]
    result = {
        **left,
        "manuscript": _merge_passages(left["manuscript"], right["manuscript"]),
        "source": _merge_passages(left["source"], right["source"]),
        "aligned_pairs": [list(pair) for pair in ordered],
        "scored_word_positions": scored,
        "matched_words": len(pairs), "included_words": len(scored),
        "excluded_words": len(pairs) - len(scored), "excluded_from_score": not scored,
        "kind": "exact" if 2 * len(pairs) == span else "near-verbatim",
        "similarity": round(2 * len(pairs) / span, 4),
        "flags": sorted(set(left.get("flags", [])) | set(right.get("flags", []))),
        "quotation": {
            **left.get("quotation", {}),
            "matched_quoted_words": len(pairs) if left.get("quotation", {}).get("status") == "recognized" else 0,
        },
        "merged_candidate_instances": left.get("merged_candidate_instances", 1) + right.get("merged_candidate_instances", 1),
        "alignment_policy": {
            "method": "canonical-paired-union", "constituents": provenance,
            "note": "Union of compatible already-qualified candidates, not a new alignment threshold. "
                    "Similarity describes exact-pair density; qualification fields retain the representative candidate. "
                    "Distinct original candidate policies and qualification summaries are listed here.",
        },
    }
    if "alternative_source_ids" in left or "alternative_source_ids" in right:
        result["alternative_source_ids"] = sorted(set(left.get("alternative_source_ids", [])) |
                                                   set(right.get("alternative_source_ids", [])))
    return result


def merge_instances(matches: list[dict], *, manuscript_regions: list[int] | None = None,
                    source_regions: list[int] | None = None,
                    check: Callable[[], None] | None = None) -> list[dict]:
    """Canonicalize overlapping same-occurrence evidence without adding matched words.

    Shared exact pairs connect candidates; their complete mappings must remain
    one-to-one and monotone. Revisit the pair index after every union so bridges
    absorb all compatible fragments, not just the first neighbor. Conflicting
    alternatives remain separate. Inputs stay untouched, including on cancellation.
    """
    active: dict[int, tuple[dict, set[tuple]]] = {}
    by_pair: dict[tuple, set[int]] = defaultdict(set)
    regions = (manuscript_regions, source_regions)

    def order_key(match):
        if check:
            check()
        return (str(match["source_id"]), -len(match.get("aligned_pairs", [])),
                tuple(tuple(pair) for pair in match.get("aligned_pairs", [])),
                json.dumps(match, sort_keys=True))

    ordered = sorted(matches, key=order_key)
    for number, original in enumerate(ordered):
        if check:
            check()
        current = original
        pairs = {tuple(pair) for pair in current.get("aligned_pairs", [])}
        while pairs:
            candidates = set().union(*(by_pair.get((str(current["source_id"]), pair), set()) for pair in pairs))
            found = None
            for candidate in sorted(candidates):
                if check:
                    check()
                previous, old = active[candidate]
                if _compatible_paths(previous, current, old, pairs, regions):
                    found = candidate
                    break
            if found is None:
                break
            previous, old = active.pop(found)
            for pair in old:
                key = (str(previous["source_id"]), pair)
                by_pair[key].remove(found)
                if not by_pair[key]:
                    del by_pair[key]
            pairs |= old
            current = _merge_paths(previous, current, pairs)
        active[number] = (current, pairs)
        for pair in pairs:
            by_pair[(str(current["source_id"]), pair)].add(number)
    output = []
    for match, _ in active.values():
        if check:
            check()
        output.append(deepcopy(match))
    return sorted(output, key=lambda m: (
        str(m["source_id"]), m["manuscript"]["match_start"], m["source"]["match_start"],
        tuple(tuple(pair) for pair in m.get("aligned_pairs", [])),
    ))


def numeric_prefix(left: str, right: str) -> int:
    """Return an actually equal decimal prefix, never a wildcard or numeric equality."""
    if left == right or not NUMERIC.fullmatch(left) or not NUMERIC.fullmatch(right):
        return 0
    limit = 0
    for a, b in zip(left, right):
        if a != b:
            break
        limit += 1
    # A whole integer prefix is not evidence that two different integers match.
    prefix = left[:limit]
    return limit if "." in prefix or "," in prefix else 0


def ordered_instances(mw: list[str], sw: list[str], ms: int, me: int, ss: int, se: int,
                      policy: dict, boundary_regions: list[int], check) -> list[tuple]:
    """Extend source-local seeds into ordered subchains under one declared policy.

    Exact word blocks provide lexical anchors. Numeric prefixes are inspectable
    character evidence only and never satisfy the exact-word minimum.
    """
    check()
    gap = int(policy["maximum_gap"])
    left = mw[ms:me]
    right = sw[ss:se]
    blocks = [block for block in SequenceMatcher(None, left, right, autojunk=False).get_matching_blocks() if block.size]
    candidates = []
    for start in range(len(blocks)):
        check()
        pairs = []
        first = blocks[start]
        for end in range(start, len(blocks)):
            block = blocks[end]
            if end > start:
                previous = blocks[end - 1]
                if max(block.a - previous.a - previous.size, block.b - previous.b - previous.size) > gap:
                    break
            pairs.extend((ms + block.a + j, ss + block.b + j) for j in range(block.size))
            ma, mb = ms + first.a, ms + block.a + block.size
            sa, sb = ss + first.b, ss + block.b + block.size
            if boundary_regions[ma] != boundary_regions[mb - 1]:
                break
            count = len(pairs)
            if count < policy["minimum_equal_words"]:
                continue
            lexical = sum(len(mw[a]) for a, _ in pairs if not NUMERIC.fullmatch(mw[a]))
            density = 2 * count / (mb - ma + sb - sa)
            exact = count == mb - ma == sb - sa
            if not exact and (lexical < policy["minimum_lexical_characters"] or density < policy["minimum_density"]):
                continue
            # Partial numeric characters are assessed only in equal-length gaps
            # between already established ordered anchors. They cannot create anchors.
            partials = []
            if policy.get("numeric_partial_credit"):
                for (a, b), (na, nb) in zip(pairs, pairs[1:]):
                    if na - a != nb - b or na - a > gap + 1:
                        continue
                    for k in range(1, na - a):
                        length = numeric_prefix(mw[a + k], sw[b + k])
                        if length:
                            partials.append((a + k, b + k, length))
            candidates.append({
                "ma": ma, "mb": mb, "sa": sa, "sb": sb, "pairs": set(pairs),
                "density": density, "exact": exact, "partials": partials,
            })
    # Retain maximal same-location instances, not every nested subchain. Distinct
    # locations and alternative source files are never collapsed into each other.
    candidates.sort(key=lambda item: (-len(item["pairs"]), -item["density"], item["ma"], item["sa"]))
    selected = []
    for item in candidates:
        if any(item["pairs"] <= previous["pairs"] for previous in selected):
            continue
        selected.append(item)
    output = []
    for item in selected:
        detail = {
            "method": "ordered-local-instance", "policy_id": policy["id"],
            "exact_token_similarity": item["density"], "maximum_gap": gap, "minimum_density": policy["minimum_density"],
            "partial_numeric_pairs": item["partials"],
            "note": "Only equal words and explicitly equal numeric prefix characters are marked; numbers are never wildcards.",
        }
        output.append(("exact" if item["exact"] else "near-verbatim",
                       item["ma"], item["mb"], item["sa"], item["sb"], item["density"],
                       {a for a, _ in item["pairs"]}, {b for _, b in item["pairs"]}, detail))
    return output
