"""One presentation state for saved and newly generated comparison results."""


def result_state(report: dict) -> dict:
    metrics = report.get("metrics") or {}
    coverage = report.get("coverage") or {}
    outcomes = {str(row.get("source_id")): row for row in report.get("source_coverage", [])}
    papers = report.get("papers") or []
    complete = partial = unchecked = excluded = 0
    for paper in papers:
        status = outcomes.get(str(paper.get("id")), {}).get("status") or paper.get("comparison_status") or paper.get("status")
        if status == "compared":
            complete += 1
        elif status in {"compared-with-limits", "partial", "partially_compared"} or paper.get("partial_comparison"):
            partial += 1
        elif str(status).startswith("excluded") or paper.get("excluded"):
            excluded += 1
        else:
            unchecked += 1
    if not papers:
        complete = coverage.get("compared", metrics.get("sources_compared", 0))
        partial = coverage.get("partially_compared", 0)
    examined = complete + partial
    eligible = metrics.get("eligible_words", 0)
    denominator = metrics.get("score_denominator_words", eligible)
    limited = bool(partial or metrics.get("truncated"))
    if not examined:
        state, heading = "not_compared", "No papers could be compared"
        explanation = "No full-text comparison was completed or partially performed. Review the file reasons below; this is not a zero-overlap result."
    elif not denominator:
        state, heading = "unscorable", "No eligible words remain after exclusions"
        explanation = "Papers were examined, but the declared exclusions leave no scoring denominator. The evidence remains available."
    elif not eligible:
        state, heading = "all_excluded", "All manuscript text was excluded from matching"
        explanation = "The submitted document has a valid total-word denominator, but no eligible text remains. Zero reflects the filters, not an originality finding."
    elif limited or unchecked:
        state, heading = "partial", "Partial comparison results"
        explanation = (
            "Comparison limits were reached. The retained findings are real comparison evidence, but not all candidate passages were checked. "
            "The displayed overlap is a lower bound, not a completed screening."
            if limited else "Some uploaded papers could not be checked. The score covers only the examined papers."
        )
    else:
        state, heading = "complete", "Your comparison report"
        explanation = "The included readable papers were compared using the saved rules. Findings require human review."
    score_available = bool(examined and denominator)
    return {
        "state": state, "heading": heading, "explanation": explanation,
        "complete_sources": complete, "partial_sources": partial, "examined_sources": examined,
        "unchecked_sources": unchecked, "excluded_sources": excluded,
        "score_available": score_available,
        "score_kind": "lower_bound" if score_available and limited else "observed" if score_available else "unavailable",
        "findings": len(report.get("matches", [])),
    }
