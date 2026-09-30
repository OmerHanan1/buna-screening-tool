# Exact + similar wording (experimental)

The hosted **Advanced** control offers `classified-v1.1` as an explicit opt-in.
The default remains **Standard wording comparison** (`validated-lexical`, engine
2.5.4). Starting a new comparison resets the selector to the standard model.
Existing reports, source extracts and comparisons are not relabeled or rerun.

## Labels and scoring

* **Exact overlap:** a maximal contiguous run of at least nine equal normalized
  word units. Case, compatible Unicode/combining forms and conservative line-end
  hyphen joins are normalized; edits or reordering are not allowed inside a run.
* **Similar wording:** lexical shared-word blocks with bounded word edits or
  reordering. It is not semantic paraphrase detection. The frozen default requires
  nine equal words, five content words, at most four unmatched words under the
  declared length-dependent allowance, and at most two out-of-order blocks.
* **No match found:** only eligible text checked against a fully processed corpus.
  It is never a finding of originality.
* **Not fully checked:** eligible text without retained matches when any source
  is unavailable, skipped or partial. Empty/all-excluded corpora cannot establish
  an unmatched finding.

Recognized quotations, bibliography and pre-Abstract manuscript content remain
separate exclusions. Citation and quotation context are independent of the
exact/similar label. If no Abstract heading is recognized, a visible warning
explains the whole-manuscript fallback.

The score uses the union of included manuscript word positions. Exact takes
precedence over similar when both cover a word; the **similar-only** ledger excludes
those exact words. Source alternatives remain in the evidence. The candidate has
a separately recorded numeric/Unicode normalization version, so its denominator
and score can differ from the standard model.

New standard comparisons use score policy `eligible-manuscript-v1`: the
denominator is the unique manuscript word positions remaining after the recognized
Abstract scope, bibliography and enabled quotation exclusions. Exclusion masks
overlap without double subtraction. Unmatched eligible words remain counted;
excluded/unavailable sources and rejected short matches do not reduce it.
The experimental model currently retains its prior scoped-total denominator,
including bibliography/quotation words. Its reports label that saved basis;
do not assume its percentages use the standard model's new denominator.
Tokenizers and matching rules have not changed, and old saved reports retain
their original denominators. The standard change is a user-selected policy,
not established Crossref arithmetic.

## PDF presentation

New experimental PDFs use restrained rose for exact wording and amber for
similar-only wording. A first-page **E/S legend**, margin markers and annotation
titles distinguish them without relying on color. Shared spans are painted once,
with exact precedence; comments retain all represented source alternatives and
their complete original passages. The comment body remains the two fields
`Source: #N` and `Overlapped text:`. Excluded passages are not painted as included
overlap. Original manuscript page geometry and matched-word coordinates remain.

Renderer 7 creates separate cached artifacts and checks that every source-table row
is rendered. Standard/legacy reports without
classification data retain their previous labeling; the renderer does not infer
new categories from old `near-verbatim` records.

## Limits and provenance

This integrates the isolated `classified-v1` candidate with correctness fixes:
warning propagation, true lazy source loading, size guards, no-source unknown
states, source-exclusion boundaries, streamed exact findings, and bounded index,
pair, candidate and alignment work. Decimal numbers remain whole units.
The numeric matching configuration is unchanged. This corrected revision is
`classified-v1.1`; earlier benchmark measurements are not accuracy evidence for it.

Per-source time, global 480-second comparison time, 64 MiB estimated retained
evidence and 128 MiB estimated working-index guards remain explicit. One atomic
similar alignment is bounded before entering its non-interruptible search.
Limits preserve retained evidence and produce partial/unknown coverage, not
silent truncation or a complete badge.

The similar path retains the best local alignment per manuscript sentence/source;
it does not promise all alternative near-match locations. Exact occurrences are
retained independently. The content-word filter uses an English stopword list.
No claim of superior overall precision/recall, Crossref equivalence or plagiarism
determination is made. The app still requires human review.

Pre-release synthetic validation of the corrected integration includes a
45-page / 12,684-word original manuscript against all 44 cached sources in a
1-vCPU / 2-GiB Linux container using pypdf 6.19.0. That run completed both paths
for all sources and generated its PDF in about 16 seconds, with a measured
container peak around 240 MB. This is a performance observation, not an accuracy
benchmark or a promise for arbitrary manuscripts. Earlier source-specific audit
figures showed a precision/recall tradeoff, not quality dominance, and must not
be presented as validation of this corrected revision.
