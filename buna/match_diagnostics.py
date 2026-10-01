"""Export saved Similar-match diagnostics without rerunning a comparison."""
import argparse
import csv
import io
import json
from pathlib import Path

FIELDS = [
    "source", "source_content_sha256", "manuscript_span", "source_span", "matched_word_count",
    "longest_exact_run", "anchor_length", "number_of_gaps", "maximum_gap", "total_gap_words",
    "local_minimum_density", "global_density", "citation_token_contribution", "reason_match_terminated", "scored",
    "matched_non_citation_words", "seed", "manuscript_density", "source_density",
    "manuscript_gap_sequence", "source_gap_sequence", "citation_matches", "citation_tokens_inside_span",
    "header_removed_words", "source_header_removed_words", "matched_quoted_words",
    "quotation_tokens_inside_span", "source_matched_quoted_words", "source_quotation_tokens_inside_span",
    "local_eligible_words", "distinct_matched_content_words", "strongest_four_word_run_content_words",
    "content_policy_version", "alignment_alternative_count", "unique_target_matched_words",
]


def alignment_details(path, mw, sw, m_cited, s_cited):
    qualifying = [(a, b) for a, b in path if not m_cited[a] and not s_cited[b]]
    ma, mb, sa, sb = path[0][0], path[-1][0] + 1, path[0][1], path[-1][1] + 1
    longest = run = 0
    previous = None
    seed = None
    for index, pair in enumerate(qualifying):
        run = run + 1 if previous and pair == (previous[0] + 1, previous[1] + 1) else 1
        longest = max(longest, run)
        if run >= 3 and seed is None:
            pairs = qualifying[index - 2:index + 1]
            seed = {"aligned_pairs": [list(p) for p in pairs], "words": [mw[a] for a, _ in pairs]}
        previous = pair
    mg = [b[0] - a[0] - 1 for a, b in zip(qualifying, qualifying[1:])]
    sg = [b[1] - a[1] - 1 for a, b in zip(qualifying, qualifying[1:])]
    # Known equal citation pairs in the supplied alignment are diagnostic only.
    citation_pairs = [[a, b] for a, b in path if m_cited[a] or s_cited[b]]
    return {
        "matched_non_citation_words": len(qualifying), "qualifying_matched_words": len(qualifying),
        "matched_word_count": len(path), "longest_exact_run": longest, "seed": seed,
        "anchor_length": 3 if seed else 0,
        "manuscript_span": [ma, mb], "source_span": [sa, sb],
        "manuscript_density": len(qualifying) / (mb - ma), "source_density": len(qualifying) / (sb - sa),
        "global_density": min(len(qualifying) / (mb - ma), len(qualifying) / (sb - sa)),
        "manuscript_gap_sequence": mg, "source_gap_sequence": sg,
        "number_of_gaps": sum(bool(a or b) for a, b in zip(mg, sg)),
        "maximum_gap": max(mg + sg, default=0), "total_gap_words": sum(mg + sg),
        "citation_matches": {"count": len(citation_pairs), "aligned_pairs": citation_pairs,
                             "basis": "Only citation pairs present in this actual alignment; no inferred citation pairing."},
        "citation_tokens_inside_span": {"manuscript": sum(m_cited[ma:mb]), "source": sum(s_cited[sa:sb])},
        "citation_token_contribution": {"similar_seed": 0, "similar_minimum": 0, "similar_density_numerator": 0,
                                        "gap_and_span_positions_preserved": True},
        "reason_match_terminated": ["retained-valid-window-under-original-order-gap-density-rules"],
    }


def raw_citation_pairs(path, mw, sw, m_cited, s_cited):
    """Diagnostic-only equal citation pairs inside already accepted prose gaps."""
    raw = [path[0]]
    for left, right in zip(path, path[1:]):
        mpos, spos = list(range(left[0] + 1, right[0])), list(range(left[1] + 1, right[1]))
        if len(mpos) > 5 or len(spos) > 5:
            raise ValueError("Raw citation diagnostics require an already accepted gap-five prose path.")
        dp = [[0] * (len(spos) + 1) for _ in range(len(mpos) + 1)]
        for a in range(len(mpos) - 1, -1, -1):
            for b in range(len(spos) - 1, -1, -1):
                i, j = mpos[a], spos[b]
                equal = mw[i] == sw[j] and (m_cited[i] or s_cited[j])
                dp[a][b] = 1 + dp[a + 1][b + 1] if equal else max(dp[a + 1][b], dp[a][b + 1])
        a = b = 0
        while a < len(mpos) and b < len(spos):
            i, j = mpos[a], spos[b]
            if mw[i] == sw[j] and (m_cited[i] or s_cited[j]):
                raw.append((i, j))
                a, b = a + 1, b + 1
            elif dp[a + 1][b] >= dp[a][b + 1]:
                a += 1
            else:
                b += 1
        raw.append(right)
    return tuple(raw)


def diagnostics_csv(report):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS)
    writer.writeheader()
    for item in report.get("improved_eng", {}).get("similar_diagnostics", []):
        row = {}
        for field in FIELDS:
            value = item.get(field)
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                value = "'" + value
            row[field] = value
        writer.writerow(row)
    return stream.getvalue()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    saved = json.loads(args.report.read_text())
    if not saved.get("improved_eng") or "similar_diagnostics" not in saved["improved_eng"]:
        parser.error("This saved report has no Similar diagnostics.")
    args.destination.write_text(diagnostics_csv(saved), encoding="utf-8")
