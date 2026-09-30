# improvedEng (experimental, opt-in)

`improvedEng` is a third lexical comparison model. Standard remains the default;
the existing standard and exact/similar engines are unchanged. Choose
**Advanced > Comparison model > improvedEng** for a new comparison. Saved reports
retain their model, configuration and score policy. No semantic model, embeddings,
LLM, or verified Crossref parameters are used.

## Frozen v1 rules

Normalize Unicode and case, tokenize punctuation/whitespace and preserve original
offsets. Numbers remain literal (`4.72` does not equal `4.16`), citation tokens
remain, and no maximum span or global diagonal-drift limit is imposed. Word
units use the existing combining-mark/decimal tokenizer. A line-break hyphen
joins only when the joined spelling is confirmed within that same document;
soft-hyphen handling follows the existing tokenizer. This document-local choice
keeps the manuscript word ledger independent of source order or availability.

Exact consecutive three-word anchors retrieve candidates but are not reportable
matches by themselves. Search preserves manuscript/source order and one-to-one
equal-word pairs. Every scored passage must have:

* At least nine equal-word pairs.
* At most five intervening unmatched tokens **on each side independently**.
* At least 60% equal-word density **on each side independently**.
* An exact three-word anchor within that passage.

Substitutions, insertions and deletions contribute no matched words. They are
recorded as unmatched words between equal pairs. No reordering is accepted.
Only actual equal-word positions are highlighted, never the gaps in a passage.

## Search and completeness

The search graph contains equal-word position pairs reachable from seeds.
Edges advance both coordinates by one to six tokens. Removing a transitive edge
is safe only when an equal pair can be inserted between its endpoints: this
retains all existing matched positions, increases density and cannot increase
either gap. All alternative saturated paths are searched, including alternative
source occurrences. All inclusion-maximal valid windows are retained, so an
invalid outer extension cannot erase a valid interior.

There is no fixed alignment band, single-best-score path, sentence restriction,
frequency exclusion or top-K truncation. Common wording and repeated words can
nevertheless generate a large graph or many paths. The existing 128 MiB
working-index, 64 MiB evidence, 120-second per-source and hosted 480-second total
comparison budgets still apply. Cancellation is checked inside expensive loops.
The implementation uses conservative memory estimates, not a guarantee that
those estimates equal process RSS.

Exact runs are collected first so real findings survive a later graph limit.
An interrupted scored search is `compared-with-limits`; its retained evidence is
a lower bound, and unmarked eligible words are **not fully checked**, not
"unmatched" or "original". An interrupted excluded-text audit is separately
reported even when the scored search finished. Missing sources are listed and
do not shrink the manuscript denominator.

Pair-compatible paths are consolidated through graph enumeration and
pair-containment deduplication. Overlapping manuscript envelopes alone never
merge incompatible source occurrences or source versions. Exact subruns remain
available for exact-first visual precedence within longer similar passages.

## Exclusions and arithmetic

Retain Abstract-onward manuscript scope and the existing visible fallback when
no Abstract is recognized. Bibliography and enabled quotation exclusions are
hard boundaries: excluded words cannot qualify or connect scored matches.
Scored search runs directly inside eligible segments, independently of raw
audit paths; each eligible passage must satisfy all v1 rules.

Discovered matches inside/crossing excluded text are retained separately in
`improved_eng.raw_excluded_evidence`, never in the scored match array or masks.
`source_coverage` reports scored-search and audit-search completeness separately.
Audit evidence is not a claim that rejected sub-nine-word fragments were all
enumerated or that raw search completed when its coverage says otherwise.

The shared `eligible-manuscript-v1` denominator excludes declared manuscript
front matter, bibliography and enabled quotations once. Otherwise eligible
unmatched words and words in rejected small matches still count. An excluded
or unavailable source does not remove manuscript words. Preprint metadata is
not inferred: the existing explicit source exclusion is supported, while
unknown preprint status remains unknown.

Overall overlap is the union of actual accepted manuscript word positions,
divided by eligible manuscript words, expressed as a percentage. Source
percentages overlap and must not be added. Zero eligible words is unscorable
with no numeric percentage.

## Reports and provenance

Model ID `improvedEng` and algorithm version `improvedEng-v1` are distinct from
existing models. Reports preserve configuration, normalization version, scope,
score policy, source extracted-content SHA-256 and ordered aligned pairs.
Local improvedEng runs do not consume old comparison checkpoints. Parsed
document caches and shared-library history are unchanged.

PDFs reuse the rose E / amber S legend with improvedEng-specific wording.
Exact means a contiguous all-equal normalized span; similar means the ordered
equal-pair rule above, not semantic paraphrasing. Citation/quotation attribution
is independent of match kind. Excluded-text audit evidence is available in JSON.

## Validation and limitations

The tiny-input oracle exhaustively enumerates all legal paths, including paths
that omit insertable pairs. Every optimized output must be legal, and every
oracle-valid pair map must be contained in an optimized valid result. Fixtures
cover both density/gap boundaries, excluded bridges, repeated words, distinct
occurrences, failed extensions, accumulated drift, actual-word unions and limits.

`deploy/verify_improved_eng.py` is an original synthetic 57/94-source workload
with 12,000 words per document, lazy loading and PDF source-row checks.
Before running it, its frozen rubric requires exact source-specific matched-word
sets, zero constructed false positives, zero missed constructed positive words,
full scored-source completion and all PDF rows. This is a correctness/performance
gate, **not** measured precision/recall on independent scientific manuscripts.

Previously explored reference reports are development data, not holdouts.
Real-paper quality must use source-linked highlighted passages on the same
available source/version subset, with a frozen passage-agreement rubric and
independently held-out manuscript. Slight differences within the same sentence
may be manually adjudicated as passage agreement, with word/character overlap
reported separately. No real-paper numerical accuracy target has been established;
no tuning against a final percentage or Crossref-equivalence claim is justified.
