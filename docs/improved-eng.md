# improvedEng: ordered alignment with precision exclusions

New comparisons use model ID `improvedEng`, algorithm version
`improvedEng-v3.1-precision`. Standard remains the default; Standard 2.5.4 and
classified-v1.1 matching and denominator behavior are unchanged. Saved reports
retain their original rules and values; neither library caches nor historical
reports are rewritten or replayed.

The narrow v3.1 follow-up counts literal numbers and statistical tokens as
meaningful Similar words and relaxes the acceptance anchor from four words/two
content words to **three consecutive words/one meaningful word**. The minimum
of four distinct shared meaningful types stays in place. No other alignment,
quotation, citation, header, Exact, denominator or evidence-budget rule changes.
This deliberately accepts more numeric/statistical wording; it is not a measured
precision/recall improvement on the unavailable user report.

## 1. Running headers and original coordinates

`repeated-numbered-page-edge-v1` derives a logical token view on both documents.
The canonical extracted text, tokens, PDF offsets and pages remain unchanged.
Only the first/last nonempty line (or edge pair with a separate counter line)
of a page with at least two body lines is considered.
A short repeated identity (2–12 words, at most 120 characters) must
occur on at least two pages with each occurrence's own page-correct numeric
counter. Odd/even identities are learned independently. Counter normalization
is for identification only. Ordinary headings, sentence punctuation, table-like
lines, mismatched page numbers and unnumbered first-page titles remain intact.

Extraction currently supplies no geometric line positions. Ambiguous unnumbered
repeated edge text is **retained with a warning**, not guessed away. This
conservative fallback cannot recognize every real running header.

Confirmed header words are absent from logical alignment and consume zero gap.
Body words on either side of a removed header can align contiguously. Every
published pair, character span and score position maps back to the original
ledger; no header rectangle is included in a merged highlight. The normalization
version changes to `nfkc-casefold-literal-numeric-document-local-hyphens-layout-v2`.

## Retained evidence and scoring priority

`live-retained-scored-priority-v1` keeps the total estimated evidence ceiling at
64 MiB: 56 MiB is protected for scored evidence and 8 MiB is reserved for the
optional excluded-text audit. Replaced temporary paths release their own
reservations. Materialized records retain their reservation, while the consumed
temporary path releases its reservation; construction overlap is still charged.
The counters measure retained representations, not the cumulative history of all
discarded candidates.

An exhausted optional audit cannot consume the scored reserve or turn an otherwise
fully checked source into a partial scored result. Audit incompleteness remains
explicit in warnings and `audit_complete`; genuine scored-evidence exhaustion
still produces partial coverage and a lower-bound score. JSON records separate
scored/audit limits, current reservations, peaks and failure reasons. This changes
resource accounting, not matching thresholds, eligible words or accepted evidence.

## Matching rules

Similar matching again uses overlapping exact **three-word retrieval seeds**,
ordered one-to-one equal-word alignments, at least **nine qualifying matched
words**, gaps of at most **five unmatched words independently per side**, and
global density of at least **60% independently per side**.

Seeds retrieve candidates; they do not qualify a reportable passage.
Alternative paths and valid inner passages are retained, not just one greedy
or maximum-score path. Front matter and bibliography remain hard boundaries.
Long quotations instead consume gap/span positions when enabled. Numerator masks use
only actual matched manuscript positions and union them across evidence/sources.
Numbers remain literal normalized tokens. There is no semantic model, embedding,
hidden span/diagonal band, top-K, beam limit or sentence cutoff.

The earlier v2 every-window density, cumulative-gap limit, all-four-content-word
anchor and global generic-language suppression are **not** restored. There is
no tuning toward an aggregate score.

## 2. APA citation qualification

Explicit bracketed numeric and author-year/narrative citation syntax is masked
in the **original word ledger**. It is not deleted or compacted. Recognition
is syntactic, not a guess about every surname or year in prose.
`apa-author-year-original-offsets-v2` recognizes parenthetical semicolon lists,
multiline author-year forms, initials, multiple authors with `&`/`and`, narrative
`Smith (2005)` / `Smith et al. (2005)`, and running `Smith et al., 2005` text.
Bare years, statistical parentheses, definitions and standalone `et al.` without
an author do not become citation spans. This recognizer is opt-in for improvedEng.

A valid seed contains three consecutive equal **non-citation eligible** words at
header-cleaned positions on both sides. Mixed citation/prose and citation-only seeds
are rejected, including at passage boundaries. Citations are not removed to
create artificial three-word adjacency. Function words and literal numbers in
ordinary prose remain eligible for retrieval.

Every accepted Similar alignment must independently contain nine equal
non-citation word pairs. Citation tokens on either side cannot increase the
minimum-match count or density numerator. They remain intervening tokens:
five citation words consume a five-word gap, six break it, and nine prose
matches spanning sixteen original tokens fail 60% density. Citations therefore
cannot rescue prose that fails the original gap/density rules.

Citation text can remain in passage context but is not painted as qualifying
Similar evidence. `qualifying_aligned_pairs` are the pairs that satisfy the
model's qualification rule. `raw_aligned_pairs` may additionally include equal
citation pairs aligned inside already accepted gaps for diagnostics only.
`citation_matches` reports those actual diagnostic pairs, while
`citation_tokens_inside_span` counts all citation material inside the span.
All Similar gaps and density numerators use only qualifying non-citation pairs,
never these extra raw pairs. Raw diagnostic pairing cannot rescue a failed match.

**Exact citation credit is unchanged** inside contiguous eligible runs of at
least nine normalized words, after header removal. Exact diagnostic rows
explicitly identify this exception: seven prose words plus two contiguous equal
citation words qualify as nine Exact words, but not nine Similar words.
The citation correction applies to Similar,
not a silent modification of the independent Exact path.

## 4. Individual quotation exclusion

Balanced quotes containing at most three tokenizer words are ignored for
exclusion, including single/double/curly/nested technical names. A long outer
quote still includes its nested short quotes. Unbalanced delimiters do not
exclude the rest of the document. The existing parser's explicit blockquote
syntax is retained.

With quotation exclusion on, longer recognized quotes on **both documents**
cannot seed, score or count toward the nine-word/density numerator. Their
positions remain in alignment: five quoted words consume a five-word gap, six
break a path. Eight eligible words plus any excluded quote still cannot qualify;
a full path with additional eligible words may qualify under the unchanged
global density rule. Attribution intersects actual matched positions, not the
surrounding context sentence. Original source excerpts are not rewritten.
With exclusion off, all quotation words are eligible (normal APA rules still
apply). Header/front/bibliography/long-quote denominator overlaps count once.

## 3. Similar content and contiguous-run acceptance

In addition to the three/nine/five/60% retrieval policy, a Similar path needs
**four distinct shared matched meaningful words**, plus a **three-word contiguous
equal run containing at least one meaningful word**. Contiguity is required on
both header-cleaned streams, never after stripping citations or quotes.
The guard never applies to Exact, even generic scientific wording or numbers.

`literal-meaningful-types-numbers-statistics-v2` is frozen in `improved_eng.py`:
an alphabetic normalized token of at least two characters is meaningful unless
in the existing function-word set. The statistics tokens
`b d f m n p r t z df sd se sem ci es η β χ μ σ ρ` and literal numeric tokens
matching `\d+(?:[.,]\d+)*` are also meaningful. Existing casefolding makes
`CI`, `M` and `SD` match their lowercase forms. Numeric values remain literal:
`0.05` does not match `0.06`, and there is no numeric wildcard or new number
normalization. Different numbers count as distinct types only if each actually
matches on both sides. Repeated equal numbers count once toward distinctness.
Mixed alphanumeric tokens are not newly meaningful. Citation tokens, including
years, remain ineligible for Similar seeds, meaningful types, nine-word minimum
and density. There is no stemming: `level` and `levels` remain different types.
Unmatched context words cannot satisfy either requirement.

The saved `distinct_matched_content_words` diagnostic retains its field name
for compatibility but uses the saved content-policy version's definition.
v3.1 records `strongest_three_word_run_meaningful_words`; old four-word
diagnostics remain readable/exportable without recomputing saved reports.
The algorithm/content-policy versions distinguish matching behavior; the
normalization and eligibility versions are unchanged because neither token
normalization nor denominator masks changed.

## 5. Evidence consolidation

Identical paths are discarded before reservation. Overlapping compatible paths
are unioned only when they share an actual aligned pair and the union remains
one-to-one, ordered and accepted under the complete rules. There is no gap
filling, joining independent source occurrences or silent loss of equal pairs.
Exact subruns retain independent precedence regardless of collection order.

Before string/JSON materialization, same-source/same-target-span/same-source-span
paths share one record. Incompatible paths retain their compact original-ledger
pair lists in `alternative_alignments`; `aligned_pairs` is the deterministic
longest representative, not a fabricated union alignment. `scored_word_positions`
and equal highlight spans union the actual alternatives. Every alternative was
accepted independently. Distinct source occurrences and other source IDs remain
separate evidence; the reader's existing target-passage groups navigate source
locations rather than rendering repeated cards.

`match_records`, `alignment_alternatives`, `distinct_target_match_spans` and
`overlapping_words` distinguish records, paths, spans and unique scored word
positions. Raw audit evidence uses the same consolidation and its separate
budget. No top-K or source-coverage reduction is introduced.

## Diagnostics and source completion

Each scored or raw excluded alignment records:

* Source identity/content hash and manuscript/source word spans.
* Matched non-citation words and longest qualifying exact run.
* The actual three-word retrieval seed and its original pairs.
* Density on each side and each side's complete gap sequence.
* Citation pairs in the alignment and citation tokens inside each span.
* Header and quote counts, local eligible words, distinct matched content types,
  three-word anchor strength, alternative count and original/logical spans.

Per-source rejection counters identify the distinct-content or three-word-anchor
qualification stage without storing rejected candidate text.

Similarity diagnostics remain downloadable as JSON/CSV through the existing
report ownership gate. Exact alignments also have diagnostic objects for
source-linked A/B/C/D evaluation. CSV neutralizes spreadsheet formula prefixes.

```sh
python -m buna.match_diagnostics report.json similar-diagnostics.csv
```

The existing resource guards, fair remaining-source time allocation and separately
bounded raw audit remain. No production limit is raised and no changed matching
threshold is hidden inside scheduling. If any scored search is interrupted,
that source stays partial; not-visited words are not labeled unmatched.
Evidence/diagnostic memory exhaustion preserves a valid partial report rather
than looking up a missing diagnostic entry and losing the report.

The shared `eligible-manuscript-v1` **arithmetic** remains unchanged. The explicit
new `eligibility_profile: improvedEng-layout-longquotes-v1` records this model's
header/short-quote/long-quote masks without changing other models. Unmatched
eligible words, citations and rejected small matches still count.
Excluded/unavailable sources never remove manuscript denominator words.

## Source-linked A/B/C/D evaluation

Calibration refuses partial coverage and requires all **61 sources** fully
compared by default, matching manuscript ledger, source content/version hashes,
and the same Abstract-onward/bibliography/quotation exclusion settings. Reference
annotation completeness must be explicitly confirmed; missing labels are not
silently treated as negative evidence.

```sh
python -m buna.span_evaluation report.json gold.json passage-agreement.json \
  --source-documents extracted-sources.json
```

`gold.json` contains:

* `annotation_complete: true`.
* `manuscript_ledger_sha256` matching the actual labeled manuscript.
* `source_manifest`: source ID to extracted-text SHA-256.
* `exclusions`: saved `score_policy_version`, `exclude_quotes`, `manuscript_scope`,
  and `eligibility_profile` when present. Labels from an old profile cannot
  silently calibrate the new one.
* `passages`: records with `source_id`, `word_start`, `word_end`,
  `source_word_start`, `source_word_end`, all half-open original-ledger bounds.

`extracted-sources.json` is an offline mapping of source IDs to their original
extracted document objects. Content hashes are checked before any diagnostic
alignment. These files are private evaluation inputs, not repository assets.

Categories:

| Category | Meaning |
|---|---|
| A | Crossref + improvedEng: shared passage |
| B | Crossref only: reference passage needing recall investigation |
| C | improvedEng only: detector passage needing precision investigation |
| D | Partial disagreement: same source occurrence/passage, different wording coverage |

The explicit provisional **evaluation rubric**, not a detector threshold,
associates overlapping manuscript/source occurrences when each overlap covers
at least half the shorter span. Connected reference/detector groups account for
split highlights without forcing one-to-one passage labels. Word-set Jaccard
agreement of 0.8 or boundary-only differences of at most two words count as A;
larger differences count as D, not a whole false positive plus a whole miss.
The rubric is emitted with results for review; source-occurrence bounds are
required so a different location in the same paper is not silently credited.

C/D diagnostics come from actual saved detector pairs. B diagnostics use an
explicitly labeled, bounded **diagnostic-only LCS** inside the reference's
original manuscript/source bounds, excluding nonqualifying words while preserving
their coordinates. This does not run in detection, change scores, or replace
the detector's alternative-path search. A probe over 200,000 cells is marked
unavailable rather than silently truncated.

If actual source text is missing, B diagnostics are marked unavailable and
`calibration_complete` is false. No diagnostic alignment is invented from PDF
rectangles or source names. Strict word precision/recall and source-specific word
sets remain available under `word_metrics`; `--word-only` exports the older
word-only view. Passage-level D is intentionally not equivalent to a complete
word-level miss.

## PDF mapping and current evidence limits

PDF renderer 8 and its independent exact/unique mapping recovery are retained.
Mapping failures remain explicit in hosted `pdf_mapping` JSON and local mapping
JSON, and never change detection scores. Saved reports are not recomputed when
a PDF is regenerated.

The exact `paper-overlap-report (3).pdf`, evidence JSON, verified 61-source
snapshot and source-linked reference labels were not supplied in this session.
Synthetic completion and regression tests are not a substitute for running that
actual case or producing its B/C/D examples. No further precision/recall tuning
or assertion of vendor equivalence is justified until those diagnostics exist.
