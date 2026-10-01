# improvedEng: ordered alignment with precision exclusions

New comparisons use model ID `improvedEng`, algorithm version
`improvedEng-v3.2-precision`. improvedEng is the default for new comparisons; Standard 2.5.4 and
classified-v1.1 matching and denominator behavior are unchanged. Saved reports
retain their original rules and values; neither library caches nor historical
reports are rewritten or replayed.

v3.2 adds literal operator units, complete identical APA-unit credit and
conservative weak-edge trimming, in that order. The v3.1 requirements of four
distinct meaningful types and a noncitation three-word run with one meaningful
word stay in place. These are explicit application-policy choices, not vendor
facts or measured precision/recall changes on the unavailable user report.

## 1. Literal operator word units

`literal-operators-equals-less-greater-approx-v1` merges the four literal
characters `=`, `<`, `>` and `≈` into the existing lexical token ledger on both
documents. Each has its actual one-character offset. They count toward nine
matched units, spans, gaps, density and the eligible denominator, but **not**
toward meaningful types or meaningful anchor words. No number wildcard exists.
`M=0.06` and `M=0.08` share `M` and `=`, not their different numeric values.

`<=`, `>=` and `==` contain two literal units; `!=` contributes only `=`.
There is no added equivalence for `≤`, `≥`, `≠`, fullwidth symbols or other
mathematical encodings. Whitespace/punctuation never becomes synthetic content.
The original cache bytes and PDF coordinates remain unchanged. The word ledger
hash and normalization/eligibility profile change only for new improvedEng runs.
Short-quotation length is measured with these same tokenizer units.

## Running headers and original coordinates

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
version is `nfkc-casefold-literal-numeric-document-local-hyphens-layout-operators-v3`.

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
`apa-author-year-original-offsets-v3` recognizes parenthetical semicolon lists,
multiline author-year forms, initials, multiple authors with `&`/`and`, narrative
`Smith (2005)` / `Smith et al. (2005)`, and running `Smith et al., 2005` text.
Bare years, statistical parentheses, definitions and standalone `et al.` without
an author do not become citation spans. This recognizer is opt-in for improvedEng.

A valid seed contains three consecutive equal **non-citation eligible** words at
header-cleaned positions on both sides. Mixed citation/prose and citation-only seeds
are rejected, including at passage boundaries. Citations are not removed to
create artificial three-word adjacency. Function words and literal numbers in
ordinary prose remain eligible for retrieval.

`complete-identical-apa-unit-v1` permits credit only for an entire author/year
unit whose normalized words match contiguously in both original ledgers.
Every author, initial, `et al.`, conjunction word and year/suffix in that unit
must match. No gaps, partial author suffix, equal year alone, bag-of-authors
comparison, deleted header or quotation/bibliography boundary can create
credit. An identical complete unit inside otherwise different semicolon lists
may qualify independently. Introductory `e.g.`/`see` tokens do not qualify as
part of that author/year unit. Bracketed numeric references are not APA units.

Verified units are atomic alignment chains: a path/window can take the whole
unit or none of it. They count toward the nine-unit minimum, density numerator
and scored positions, but never toward meaningful types or the noncitation
retrieval/acceptance anchor. Partial or unmatched citation words stay excluded
and consume gaps/spans exactly as before. Thus five unverified citation units
consume five gap positions and six break a path; no zero-width citation join
is introduced. Citation-only Exact runs of at least nine remain supported;
there is no citation-only Similar retrieval.

Verified citation words are painted at their original offsets.
`qualifying_aligned_pairs` includes verified complete units plus eligible
noncitation pairs. `raw_aligned_pairs` may additionally include equal
citation pairs aligned inside already accepted gaps for diagnostics only.
`citation_matches` reports those actual diagnostic pairs, while
`citation_tokens_inside_span` counts all citation material inside the span.
All Similar gaps and density numerators use only the qualifying pairs, never
the extra raw pairs. `qualifying_prose_words`, `verified_citation_words`,
`verified_citation_pairs` and `excluded_citation_pairs` distinguish the roles.
Raw diagnostic pairing cannot rescue a failed match.

**Exact citation credit is unchanged** inside contiguous eligible runs of at
least nine normalized words, after header removal. Exact diagnostic rows
explicitly identify this exception: seven prose words plus two contiguous equal
citation words qualify as nine Exact words. They may now qualify Similar only
when those citation words form a complete verified unit and the unchanged
meaningful/anchor/gap/density guards also pass. No additional restriction is
applied to the independent Exact path.

## 3. Weak aligned-edge padding

`weak-one-two-unit-fringe-v1` removes only a contiguous **one- or two-pair outer
run**, separated from the next run by a positive gap on either side, when every
word in that outer run is nonmeaningful and no pair belongs to a complete
verified citation unit. Repeat at either edge as necessary. Typical removable
fringes are a detached `the`, `of the`, or operator-only fragment.

One/two meaningful edge words, verified full citations, runs of at least three
stopwords and every internal run/gap remain untouched. Genuinely contiguous
Exact evidence is unchanged. This deliberately does not trim every short edge.
Trimming occurs before candidate windows and acceptance; shortened windows
are re-evaluated, and compatible merges cannot restore trimmed padding.
All minima, spans, densities, score positions and highlights use the resulting
alignment, not the context sentence. A fringe that previously supplied the
ninth unit can no longer rescue an eight-unit match. Per-source
`trimmed_edge_pair_occurrences` is a processing count, not unique removed words.

## Individual quotation exclusion

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

## Similar content and contiguous-run acceptance

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
years, remain ineligible for Similar seeds and meaningful types; only verified
whole APA units receive minimum/density credit. The four operator characters
are not meaningful. There is no stemming: `level` and `levels` remain different types.
Unmatched context words cannot satisfy either requirement.

The saved `distinct_matched_content_words` diagnostic retains its field name
for compatibility but uses the saved content-policy version's definition.
v3.1 and later record `strongest_three_word_run_meaningful_words`; old four-word
diagnostics remain readable/exportable without recomputing saved reports.
The algorithm, word, citation-credit, edge and eligibility versions distinguish
the new behavior without rewriting historical reports.

## Evidence consolidation

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
* Matched operator units, verified whole-citation versus excluded citation
  pairs, qualifying prose count and weak-edge policy.

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
new `eligibility_profile: improvedEng-layout-longquotes-operators-v2` records this
model's operator-inclusive ledger and header/short-quote/long-quote masks
without changing other models. Unmatched
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
