# improvedEng: restored ordered alignment with citation qualification

New comparisons use model ID `improvedEng`, algorithm version
`improvedEng-v1-citation`. This restores the previous flexible ordered-alignment
milestone rather than tuning v2. Standard remains the default; the other engines
are unchanged. Saved v1/v2 reports retain their own rules and values.

## Restored matching rules

Similar matching again uses overlapping exact **three-word retrieval seeds**,
ordered one-to-one equal-word alignments, at least **nine qualifying matched
words**, gaps of at most **five unmatched words independently per side**, and
global density of at least **60% independently per side**.

Seeds retrieve candidates; they do not qualify a reportable passage.
Alternative paths and valid inner passages are retained, not just one greedy
or maximum-score path. Exclusions remain hard boundaries. Numerator masks use
only actual matched manuscript positions and union them across evidence/sources.
Numbers remain literal normalized tokens. There is no semantic model, embedding,
hidden span/diagonal band, top-K, beam limit or sentence cutoff.

The v2 four-content-word anchor, every-window density, cumulative-gap limit,
informative-word requirement and generic/academic-language suppression are
removed from the implementation. Formulaic Methods/Results wording can match.
The acceptance thresholds above are frozen until actual B/C/D diagnostics are
reviewed. No parameter is tuned toward a 13% aggregate score.

## The sole Similar precision correction: citations

Explicit bracketed numeric and author-year/narrative citation syntax is masked
in the **original word ledger**. It is not deleted or compacted. Recognition
is syntactic, not a guess about every surname or year in prose.

Citation-only seeds cannot retrieve candidates. A mixed prose/citation exact
three-word seed may retrieve a candidate, but its citation words do not qualify
any equal-pair graph nodes. This follows "citations must not form the seed by
themselves" without adding a new all-content or all-prose anchor restriction.

Every accepted Similar alignment must independently contain nine equal
non-citation word pairs. Citation tokens on either side cannot increase the
minimum-match count or density numerator. They remain intervening tokens:
five citation words consume a five-word gap, six break it, and nine prose
matches spanning sixteen original tokens fail 60% density. Citations therefore
cannot rescue prose that fails the original gap/density rules.

Citation text can remain in passage context but is not painted as qualifying
Similar evidence. Diagnostic `citation_matches` reports citation pairs present
in the actual saved alignment, not a guessed pairing of surrounding citations.
For Similar this count is zero by construction; `citation_tokens_inside_span`
separately exposes the citation material consuming the original span.

**Exact matching is unchanged**, including its treatment of citation text inside
contiguous exact runs of at least nine normalized words. Exact diagnostic rows
explicitly identify this exception; the citation correction applies to Similar,
not a silent modification of the independent Exact path.

## Diagnostics and source completion

Each scored or raw excluded alignment records:

* Source identity/content hash and manuscript/source word spans.
* Matched non-citation words and longest qualifying exact run.
* The actual three-word retrieval seed and its original pairs.
* Density on each side and each side's complete gap sequence.
* Citation pairs in the alignment and citation tokens inside each span.

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

The shared `eligible-manuscript-v1` denominator remains unchanged. Unmatched
eligible words, citation words and words in rejected small matches still count.
Front matter, bibliography and enabled quotation exclusions are counted once.
Excluded/unavailable sources do not remove manuscript denominator words.

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
* `exclusions`: saved `score_policy_version`, `exclude_quotes`, `manuscript_scope`.
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
