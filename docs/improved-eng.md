# improvedEng-v2: anchored Similar matching (experimental)

The model selector is still `improvedEng`; new reports record algorithm version
`improvedEng-v2`. Standard remains the default. Neither other engine is changed,
and saved v1 reports retain their original rules and values.

**Exact matching is unchanged:** contiguous equal normalized runs of at least
nine words are collected independently, including long formulaic wording.
Only the Similar path is tightened. These parameters are provisional lexical
hypotheses, not published Crossref/iThenticate parameters or a parity claim.

## Separate anchor strength and extension tolerance

Similar starts from **four contiguous exact matching content words**.
Each must be alphabetic, at least three characters and outside the existing
function-word set. Punctuation is not a word. Citation tokens cannot anchor.

To reduce generic anchors without a phrase blacklist, at least two anchor words
must not be common in both documents. A common word occurs at least three times
and at least 0.5% of noncitation tokens in that document. This is an explicitly
reported **document-pair frequency proxy**, not a full-corpus document-frequency
model. It avoids a corpus prepass, source-order dependence and silently changing
the word ledger when sources are unavailable. Real-corpus calibration remains
necessary, especially for short or specialized papers.

Each seed extends left and then right through one-to-one, ordered equal prose
words. Alternative continuations are retained within explicit resource limits.
At every extension step the candidate must satisfy:

| Constraint | Provisional v2 setting |
|---|---|
| Minimum qualifying equal words in a final match instance | 9 |
| Largest intervening prose gap | 2 independently per side |
| Total unmatched prose words | 6 independently per side |
| Number of gap events | 3 |
| Moving local density | At least 70% in every 12-token window, on each side |
| Shorter-than-12 spans | Apply 70% to the entire shorter span |
| Whole-span density | At least 70% on each side |
| Informative local support | At least two matched noncommon content words in each moving window |
| Citation interruption | At most 16 citation tokens between matched prose words |

Every moving window is checked, not just disjoint windows or the final global
average. There is no sentence restriction or maximum total-span cutoff, but
cumulative gaps prevent weak islands from chaining indefinitely. Strong end
regions cannot compensate for a weak middle. A branch stops when extension
violates continuity; its valid inner passage survives. This is not maximum-score
edit alignment and does not claim to enumerate all possible proprietary matches.

No nearby-passage union is performed. Pair-containment deduplication removes
redundant candidates only; distinct or incompatible source occurrences remain.
Exact subruns retain display precedence inside longer Similar passages.

## Citations, normalization and scoring

Explicit bracketed numeric and author-year/narrative citation syntax is recognized.
Ordinary surnames without citation syntax are not guessed to be citations.
Citation tokens contribute **zero** to Similar anchors, minimum qualifying
matched words, local/global density and Similar scoring. They may be crossed
within the interruption bound; diagnostics retain their inside-passage counts.
They remain visible as passage context but are not painted as matched words.
Exact evidence, including exact citation text, is unchanged by this Similar policy.

Numbers remain literal; isolated differences are tolerated as unmatched prose,
not treated as equal. Existing Unicode/case, decimal, punctuation, original-offset,
and document-local line-hyphen normalization is unchanged.

Abstract-onward scope, recognized bibliography and enabled quotations remain
hard eligibility boundaries. Excluded words cannot connect scored evidence.
Discovered excluded-text matches are separate raw audit evidence in JSON.

The shared `eligible-manuscript-v1` denominator is unchanged. Otherwise eligible
unmatched words, citation words, and words in rejected short matches remain in
the denominator. Missing/excluded sources do not shrink it. The numerator is
the union of actual accepted manuscript positions, never enclosing passage
bounds or the sum of per-source percentages. Zero denominator is unscorable.
Preprint status is not guessed.

## Completion and resource limits

No production limits are raised. The hosted total remains 480 seconds, each
source at most 120 seconds, working index 128 MiB and evidence 64 MiB.
Remaining comparison time is fairly apportioned over the remaining selected
sources instead of allowing early sources to consume the entire deadline.
Excluded-text auditing gets at most 10% of the current source allocation and
cannot silently consume all time reserved for later sources.

An interruption remains explicit: `compared-with-limits`, skipped or unavailable.
Scored search and raw audit completeness are separate fields. The percentage
on an incomplete report remains a lower bound, not an all-source final score.
Fair scheduling is not a guarantee of completing an arbitrary 61-source workload.
`improved_eng.calibration_ready` is false unless every selected source is fully
compared. Partial sources are never used to label unmarked words "unmatched."

## Diagnostics and downloads

Each Similar match has a `diagnostics` object. The same objects are collected
in `improved_eng.similar_diagnostics`, including raw excluded candidates with
`scored: false`. Fields include source/content hash, manuscript/source word spans,
matched-word count, longest exact run, anchor length/pairs/strength, gap count,
maximum/total gap words, per-side and minimum local/global density,
citation-token contribution, and termination reasons.

The normal JSON report includes these records. The UI offers
**Download Similar diagnostics CSV** for v2 reports using the same report
ownership gate. A local export also works without rerunning detection:

```sh
python -m buna.match_diagnostics report.json similar-diagnostics.csv
```

The fixed profile and normalization version are saved, so future calibration can
compare explicit parameter versions rather than silently reinterpret old reports.

## Four-set word/span evaluation

The evaluator refuses incomplete selected-source coverage. By default it requires
**all 61 sources** fully compared. It also verifies the manuscript word-ledger
hash, source extracted-content/version hashes and exclusion/scope settings.

```sh
python -m buna.span_evaluation report.json gold.json agreement.json
```

Gold JSON must contain:

* `annotation_complete: true`: explicit confirmation that absence means a
  negative label, not an unannotated part of the reference.
* `manuscript_ledger_sha256`: copied from the correctly matched report after
  verifying it is the original labeled manuscript.
* `source_manifest`: object mapping each source ID to its extracted-content SHA-256.
* `exclusions`: `score_policy_version`, `exclude_quotes`, and the saved
  `manuscript_scope` object.
* `passages`: source-linked `{source_id, word_start, word_end}` labels using the
  same ledger, with half-open word bounds. Do not use unverified PDF rectangle
  bounds as word labels.

After masking excluded words, every source gets four explicit interval sets:
Crossref + Detector, Crossref only, Detector only, Neither. Precision, recall
and F1 count actual source-specific word coverage; a matching word attributed
only to a different source is not silently credited. Neither counts source-word
opportunities. Empty precision/recall denominators are null, not fabricated 100%.
Small wording differences in the same sentence can also be manually adjudicated
as passage agreement, but that is distinct from these exact word-span metrics.

Do not tune a final percentage toward 13%. Freeze the labeling rubric and model
profile before independent evaluation; previously inspected reference material is
development data, not an independent holdout.

## PDF mapping is a separate subsystem

Renderer version 8 caches normalized page/glyph maps. If the two PDF extractors
disagree at a header, column boundary or footnote, short words can additionally
map inside an **exact block unique in both extractions**. No fuzzy-score placement
or guessed repeated occurrence is allowed. Existing exact-page/unique-context
mappings remain available.

Mapping failure never changes detector evidence or the overlap score. Hosted
reports now retain the renderer's full `pdf_mapping` manifest in downloadable JSON
(previously the hosted worker discarded that return value), including failed
ranges, page/source IDs, reasons and method counts. Local mapping JSON remains
available separately. A renderer version change invalidates only PDF caches;
it does not rerun detection or reinterpret saved scores.

The specific report named "new detector report 21.7.pdf", its evidence JSON,
verified 61-source input manifest and source-linked Crossref labels were not
available during this revision. Synthetic tests validate the changes, but do
not establish that its 52 unmapped ranges are repaired, that its actual 61-source
comparison completes, or that real-reference precision/recall has improved.
