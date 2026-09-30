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
]


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
        parser.error("This saved report has no improvedEng-v2 Similar diagnostics.")
    args.destination.write_text(diagnostics_csv(saved), encoding="utf-8")
