export interface Reference {
  id: string;
  raw: string;
  doi?: string;
  title?: string;
}

export interface Paper {
  id: string;
  title?: string;
  doi?: string;
  url?: string;
  pdf_url?: string;
  origins?: string[];
  status: string;
  error?: string;
  warnings?: string[];
  compared?: boolean;
  partial_comparison?: boolean;
  comparison_status?: string;
}

export interface Segment {
  text: string;
  page?: number;
  pages?: number[];
  section?: string;
  start?: number;
  end?: number;
  match_start?: number;
  match_end?: number;
  highlights?: [number, number][];
}

export interface Document {
  title?: string;
  text?: string;
  segments?: Segment[];
  pages?: number | Array<string | { text?: string; page?: number }>;
  warnings?: string[];
  word_count?: number;
  [key: string]: unknown;
}

export interface Job {
  id: string;
  title: string;
  status: string;
  stage: string;
  progress: number;
  message?: string;
  created_at: string;
  updated_at: string;
  warnings?: string[];
  queries?: string[];
  references?: Reference[];
  target?: number;
  mode?: string;
  papers?: Paper[];
  document?: Document;
  report_available?: boolean;
  coverage?: Partial<Record<string, number>>;
  is_test_fixture?: boolean;
}

export interface Provider {
  id: string;
  label: string;
  configured: boolean;
  description: string;
}

export interface Consent {
  token: string;
  destinations: string[];
  payload: {
    queries: string[];
    references: Reference[];
    target: number;
    provider: string;
    [key: string]: unknown;
  };
  description: string;
}

export interface Match {
  source_id: string;
  kind: string;
  manuscript: Segment;
  source: Segment;
  similarity: number;
  flags?: string[];
  manuscript_highlights?: [number, number][];
  source_highlights?: [number, number][];
}

export interface Report {
  metrics: {
    eligible_words: number;
    overlapping_words: number;
    overlap_percent: number;
  };
  matches: Match[];
  warnings?: string[];
  methodology?: unknown;
  papers?: Paper[];
  references?: Reference[];
  score_available?: boolean;
  screening_summary?: string;
  source_coverage?: SourceCoverage[];
  coverage?: Partial<Record<string, number>>;
}

export interface SourceCoverage {
  source_id: string;
  status: string;
  reason?: string;
  error?: string;
}
