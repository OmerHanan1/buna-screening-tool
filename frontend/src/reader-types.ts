import type { Report, Match, Paper } from "./types";

export interface ReaderSource {
  id: string;
  number: number;
  title: string;
  status: string;
  reason: string;
  overlap_percent?: number | null;
  overlapping_words?: number;
  match_indices: number[];
}
export interface ReaderFragment {
  text: string;
  start: number;
  end: number;
  match_indices: number[];
  excluded_indices: number[];
}
export interface ReaderPage {
  number: number;
  start: number;
  text: string;
  fragments: ReaderFragment[];
}
export interface ReaderReport extends Report {
  job?: { id?: string; title?: string };
  algorithm_version?: string;
  generated_at?: string;
  comparison_model?: string;
  scope?: string;
  settings?: {
    minimum_matched_words?: number; exclude_quotes?: boolean;
    manuscript_scope?: {
      requested: "abstract-onward"; applied: "abstract-onward" | "whole-document";
      start_offset: number | null; start_page: number | null; heading_text: string | null; reason: string;
    };
    [key: string]: unknown;
  };
  metrics: Report["metrics"] & {
    score_denominator_words?: number; score_basis?: string; total_words?: number;
    bibliography_words?: number; excluded_quotation_words?: number;
    front_matter_words?: number; analyzed_words?: number;
  };
  papers: Paper[];
  matches: (Match & {
    classification?: string;
    excluded_from_score?: boolean;
    included_words?: number;
    exclusion_reasons?: string[];
    citation?: { basis: string; attribution: string };
    quotation?: { status: string };
  })[];
  result: {
    state: string; heading: string; explanation: string; score_available: boolean; score_kind: string;
    complete_sources: number; partial_sources: number; unchecked_sources: number; excluded_sources: number;
  };
  reader: {
    pages: ReaderPage[];
    sources: ReaderSource[];
    groups: { id: number; match_indices: number[]; best_match_index: number; source_ids: string[] }[];
    fidelity: string;
    legacy_pages_unavailable: boolean;
  };
}
