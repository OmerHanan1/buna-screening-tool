import type { Paper, SourceCoverage } from "./types";

export interface TextFragment {
  text: string;
  highlighted: boolean;
}

export function highlightedFragments(text: string, ranges?: [number, number][]): TextFragment[] {
  if (!ranges?.length) return [{ text, highlighted: false }];
  // The Python matcher reports Unicode character offsets, not UTF-16 code units.
  const characters = Array.from(text);
  const normalized = ranges
    .filter((range) => Array.isArray(range) && range.length === 2 && range.every(Number.isInteger))
    .map(([start, end]) => [Math.max(0, start), Math.min(characters.length, end)] as [number, number])
    .filter(([start, end]) => end > start)
    .sort(([left], [right]) => left - right);
  const merged: [number, number][] = [];
  for (const [start, end] of normalized) {
    const previous = merged.at(-1);
    if (previous && start <= previous[1]) previous[1] = Math.max(previous[1], end);
    else merged.push([start, end]);
  }
  const fragments: TextFragment[] = [];
  let cursor = 0;
  for (const [start, end] of merged) {
    if (start > cursor) fragments.push({ text: characters.slice(cursor, start).join(""), highlighted: false });
    fragments.push({ text: characters.slice(start, end).join(""), highlighted: true });
    cursor = end;
  }
  if (cursor < characters.length) fragments.push({ text: characters.slice(cursor).join(""), highlighted: false });
  return fragments.length ? fragments : [{ text, highlighted: false }];
}

export function reuseCategory(kind: string): string {
  const labels: Record<string, string> = {
    exact: "Exact reuse",
    exact_sentence: "Exact sentence reuse",
    exact_passage: "Exact passage reuse",
    near: "Near-identical reuse",
    near_exact: "Near-identical reuse",
    near_identical: "Near-identical reuse",
    near_sentence: "Near-identical sentence",
  };
  return labels[kind] || kind.replaceAll("_", " ");
}

export function comparisonState(paper: Paper, coverage: SourceCoverage[] = []): "full" | "partial" | "unexamined" {
  const row = coverage.find((item) => item.source_id === paper.id);
  if (row) {
    if (row.status === "compared") return "full";
    if (row.status === "compared-with-limits") return "partial";
    return "unexamined";
  }
  if (paper.partial_comparison || paper.comparison_status === "compared-with-limits") return "partial";
  if (paper.compared === false) return "unexamined";
  return paper.status === "compared" || paper.compared === true ? "full" : "unexamined";
}

export function sourceReason(paper: Paper, coverage: SourceCoverage[] = []): string {
  const row = coverage.find((item) => item.source_id === paper.id);
  if (paper.error || row?.reason || row?.error) return paper.error || row?.reason || row?.error || "";
  const reasons: Record<string, string> = {
    "excluded-identical": "This is an identical copy of your manuscript, so it was not compared.",
    "skipped-evidence-limit": "Not examined: the comparison evidence limit was reached before this source could be compared.",
    "skipped-size-limit": "Not examined: this source exceeded the comparison size limit.",
    "compared-with-limits": "Only part of this article was compared because a comparison limit was reached. It is not a fully compared source.",
    discovered: "Only metadata was discovered; no full text was acquired for comparison.",
    resolved: "The reference was resolved, but a permitted full text was not acquired.",
    downloaded: "Full text was downloaded, but parsing and comparison did not finish.",
    parsed: "Readable full text is available, but this run did not compare it. Run the screening again.",
    unavailable: "A readable, permitted full text was unavailable. Attach a local copy in the source library.",
    excluded: "This source was excluded from comparison. No specific exclusion reason was supplied; review the source warnings.",
  };
  const status = row?.status || paper.comparison_status || (paper.partial_comparison ? "compared-with-limits" : paper.status);
  return reasons[status] || "This source was not fully compared. Review its status in the source library.";
}
