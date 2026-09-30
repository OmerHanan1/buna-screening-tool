"""Versioned manuscript-only scoring denominator; matching policy is unchanged."""

SCORE_BASIS = "eligible-manuscript-word-units"
SCORE_POLICY_VERSION = "eligible-manuscript-v1"
DENOMINATOR_DESCRIPTION = (
    "Unique eligible manuscript word positions within the saved scope: exclude front matter before the recognized Abstract, "
    "recognized bibliography, and recognized quotations only when quotation exclusion is enabled. "
    "Overlapping exclusion masks are counted once. Other declared manuscript matching exclusions are also excluded. "
    "Unmatched eligible words remain in the denominator. Source exclusions, unavailable sources and minimum-match "
    "thresholds do not remove manuscript words. This is the chosen application policy, not verified vendor arithmetic."
)


def manuscript_word_accounting(eligible, front, bibliography, quotations, *, exclude_quotes):
    lengths = {len(mask) for mask in (eligible, front, bibliography, quotations)}
    if len(lengths) != 1:
        raise ValueError("Manuscript eligibility masks must describe the same word positions.")
    counts = {"front_matter_words": 0, "excluded_bibliography_words": 0,
              "excluded_quotation_words": 0, "other_excluded_manuscript_words": 0}
    for included, before, reference, quoted in zip(eligible, front, bibliography, quotations):
        if included:
            if before or reference or (exclude_quotes and quoted):
                raise ValueError("A declared manuscript exclusion was marked eligible.")
        elif before:
            counts["front_matter_words"] += 1
        elif reference:
            counts["excluded_bibliography_words"] += 1
        elif exclude_quotes and quoted:
            counts["excluded_quotation_words"] += 1
        else:
            counts["other_excluded_manuscript_words"] += 1
    total, denominator = len(eligible), sum(bool(value) for value in eligible)
    return {
        **counts, "total_words": total, "scoped_words": total - counts["front_matter_words"],
        "analyzed_words": total - counts["front_matter_words"], "eligible_words": denominator,
        "score_denominator_words": denominator, "excluded_manuscript_words": total - denominator,
        "bibliography_words": sum(bool(value) for value in bibliography),
        "quotation_words": sum(q and not f and not b for q, f, b in zip(quotations, front, bibliography)),
        "score_basis": SCORE_BASIS, "score_policy_version": SCORE_POLICY_VERSION,
        "unscorable": denominator == 0, "all_text_excluded": denominator == 0,
    }
