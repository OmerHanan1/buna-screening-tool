"""Self-contained manuscript-first exports; saved comparison data is never changed."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from buna.presentation import manuscript_reader
from buna.result_state import result_state

_FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
_THEME = (_FRONTEND / "src" / "theme.css").read_text(encoding="utf-8")


def _escape(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _json(value: object) -> str:
    return _escape(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or any(ord(char) <= 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password:
            return None
        return _escape(value)
    except ValueError:
        return None


def _snippet(passage: dict, label: str) -> str:
    text = str(passage.get("text", ""))
    ranges = passage.get("highlights", [])
    parts, cursor = [], 0
    for start, end in sorted(ranges):
        if isinstance(start, int) and isinstance(end, int) and cursor <= start < end <= len(text):
            parts += [_escape(text[cursor:start]), "<mark>", _escape(text[start:end]), "</mark>"]
            cursor = end
    parts.append(_escape(text[cursor:]))
    pages = passage.get("pages") or [passage.get("page", "unknown")]
    return f'<div class="source-excerpt"><h3>{_escape(label)}</h3><p>Page(s) {_escape(", ".join(map(str, pages)))} · {_escape(passage.get("section", ""))}</p><blockquote>{"".join(parts)}</blockquote></div>'


def _assets() -> tuple[str, str]:
    """Embed the same reader component used by the app, without remote assets."""
    index = _FRONTEND / "dist" / "index.html"
    if not index.exists():
        return "", ""
    content = index.read_text(encoding="utf-8")
    scripts = re.findall(r'<script[^>]+src="(/assets/[^"]+)"', content)
    styles = re.findall(r'<link[^>]+href="(/assets/[^"]+\.css)"', content)
    if not scripts:
        return "", ""
    js = (_FRONTEND / "dist" / scripts[0].lstrip("/")).read_text(encoding="utf-8")
    if "buna-report-data" not in js:
        return "", ""
    css = "\n".join((_FRONTEND / "dist" / path.lstrip("/")).read_text(encoding="utf-8") for path in styles)
    return re.sub(r"</script", lambda match: match.group().replace("</", r"<\/"), js, flags=re.I), css


def render_report(report: dict) -> str:
    reader = report.get("reader") or manuscript_reader(report)
    result = result_state(report)
    payload = {**report, "reader": reader, "result": result}
    title = (report.get("job") or {}).get("title") or "Manuscript comparison"
    metrics = report.get("metrics") or {}
    denominator = metrics.get("score_denominator_words", metrics.get("eligible_words", 0))
    score = f'{_escape(metrics.get("overlap_percent", 0))}%' if result["score_available"] else "Not assessed"
    basis = {"all-submitted-word-units": "total submitted word units",
             "abstract-onward-word-units": "words from the Abstract onward"}.get(metrics.get("score_basis"), "eligible manuscript words (saved legacy basis)")
    manuscript_scope = (report.get("settings") or {}).get("manuscript_scope") or {}
    scope_notice = "Abstract heading not detected; the whole manuscript was analyzed (front matter was not excluded)." if manuscript_scope.get("requested") == "abstract-onward" and manuscript_scope.get("applied") == "whole-document" else ""
    sources = {str(source["id"]): source for source in reader["sources"]}
    matches = report.get("matches", [])
    manuscript = []
    for page in reader["pages"]:
        parts = []
        for fragment in page["fragments"]:
            indices = fragment["match_indices"]
            if indices:
                numbers = sorted({sources[str(matches[i]["source_id"])]["number"] for i in indices if str(matches[i]["source_id"]) in sources})
                references = ",".join(f'<a href="#source-{_escape(number)}">{_escape(number)}</a>' for number in numbers)
                parts.append(f'<mark>{_escape(fragment["text"])}</mark><sup>{references}</sup>')
            else:
                parts.append(_escape(fragment["text"]))
        if page["text"].strip():
            manuscript.append(f'<article class="print-manuscript-page"><h2>Manuscript page {_escape(page["number"])}</h2><div class="original-text">{"".join(parts)}</div></article>')
    legend = []
    for source in reader["sources"]:
        raw = next((paper for paper in report.get("papers", []) if str(paper["id"]) == source["id"]), {})
        url = _safe_url(raw.get("url"))
        link = f'<a href="{url}" rel="noopener noreferrer">Source link</a>' if url else ""
        pages = sorted({str(matches[i].get("source", {}).get("page", "?")) for i in source["match_indices"]})
        status = {"compared": "Checked", "compared-with-limits": "Partly checked",
                  "unavailable": "Unavailable", "excluded": "Excluded", "excluded-by-user": "Excluded",
                  "excluded-identical": "Excluded identical copy"}.get(source["status"], source["status"])
        percent = str(source["overlap_percent"]) + "%" if source["status"] in ("compared", "compared-with-limits") and source.get("overlap_percent") is not None else "Not assessed"
        legend.append(f'<tr id="source-{_escape(source["number"])}"><td>{_escape(source["number"])}</td><td>{_escape(source["title"])} {link}</td>'
                      f'<td>{_escape(status)} · {_escape(percent)}<br>{_escape(source["reason"])}</td><td>{_escape(", ".join(pages) or "—")}</td></tr>')
    appendix = []
    for index, match in enumerate(matches):
        number = sources.get(str(match["source_id"]), {}).get("number", "?")
        appendix.append(f'<article id="evidence-{index}"><h2>Source {_escape(number)} · Evidence location {index + 1}</h2>'
                        f'<p>{_escape(match.get("classification", match.get("kind", "Text overlap")))} · '
                        f'{"Excluded from score" if match.get("excluded_from_score") else "Included evidence"}</p>'
                        + _snippet(match.get("manuscript", {}), "Your original passage")
                        + _snippet(match.get("source", {}), "Matching source passage")
                        + f'<p>{_escape(match.get("citation", {}).get("basis", ""))}</p>'
                        + f'<p>{_escape("; ".join(match.get("exclusion_reasons", [])))}</p></article>')
    js, css = _assets()
    encoded = json.dumps(payload, ensure_ascii=False, default=str).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    theme_script = """(()=>{const p=new URLSearchParams(location.search).get("clawpilotTheme");
const theme=(p==="light"||p==="dark")?p:(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");
document.documentElement.dataset.theme=theme;})();"""
    print_css = """
body{background:var(--cp-bg);color:var(--cp-text);font:15px/1.6 "Segoe UI",Aptos,Calibri,-apple-system,BlinkMacSystemFont,sans-serif}
.static-report{max-width:1000px;margin:32px auto;padding:24px}.interactive-ready .static-report{display:none}
.static-report h1{font-size:28px;margin:0 0 12px}.static-report h2{font-size:18px;margin:20px 0}
.original-text{white-space:pre-wrap;overflow-wrap:anywhere}.static-report mark{background:var(--cp-surface-soft);color:var(--cp-text);border-bottom:1px solid var(--cp-border-strong)}
.static-report sup{font:10px Consolas,"Courier New",monospace;padding-left:3px}.static-report a{color:var(--cp-text)}
.static-report table{border-collapse:collapse;width:100%}.static-report td,.static-report th{padding:8px;text-align:left;vertical-align:top;border-bottom:1px solid var(--cp-border);overflow-wrap:anywhere}
.static-report th:nth-child(1){width:6%}.static-report th:nth-child(2){width:34%}.static-report th:nth-child(3){width:45%}.static-report th:nth-child(4){width:15%}
.static-report blockquote{white-space:pre-wrap;background:var(--cp-surface-soft);padding:12px;margin:8px 0}
.static-report pre{white-space:pre-wrap;overflow-wrap:anywhere;font:12px Consolas,"Courier New",monospace}
.print-evidence-appendix{display:none}
@media print{@page{size:A4;margin:15mm}body{background:var(--cp-surface);font-size:10pt}
#root{display:none!important}.static-report{display:block!important;margin:0;padding:0;max-width:none}
.no-print,.static-report>.technical-details{display:none!important}.original-text{font-size:10.5pt;line-height:1.55}
.print-manuscript-page{break-before:page}.source-legend{break-before:page}.static-report tr{break-inside:avoid}
.static-report h1{font-size:20pt}.static-report h2{font-size:12pt}.include-evidence .print-evidence-appendix{display:block;break-before:page}}
"""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<script>{theme_script}</script><title>{_escape(title)} — Paper Overlap Detector</title><style>{_THEME}\n{css}\n{print_css}</style></head><body>'
        '<div id="root"></div><main class="static-report">'
        f'<header><h1>{_escape(title)}</h1><h2>{_escape(result["heading"])}</h2><p><strong>{score}</strong> overlap{" (lower bound)" if result["score_kind"] == "lower_bound" else ""} · '
        f'{result["complete_sources"]}/{len(sources)} papers fully checked · {result["partial_sources"]} partly checked · '
        f'{result["unchecked_sources"]} not checked · {result["excluded_sources"]} excluded</p>'
        f'<p>{_escape(result["explanation"])}</p><p>{_escape(metrics.get("overlapping_words", 0))} / {_escape(denominator)} {_escape(basis)}. '
        f'Algorithm {_escape(report.get("algorithm_version", "legacy"))}. Text overlap, not a plagiarism verdict.</p>'
        f'<p>{_escape(report.get("scope", "Only the sources listed in this report."))}</p>'
        + (f'<p><strong>{_escape(scope_notice)}</strong></p>' if scope_notice else '')
        + ('<p><strong>Experimental ordered-instance model: accuracy and vendor equivalence are not established.</strong></p>' if report.get("comparison_model") == "experimental-ordered" else '')
        +
        '<p>Page-preserving extracted text, not the original PDF layout. Superscript numbers refer to source files in the legend.</p>'
        '<div class="no-print"><button onclick="window.print()">Print annotated manuscript</button> '
        '<button onclick="document.body.classList.add(\'include-evidence\');window.print();document.body.classList.remove(\'include-evidence\')">Print with evidence appendix</button></div>'
        '<p class="no-print">Interactive review uses the manuscript and source pane above. Print the annotated manuscript by default; include detailed evidence only when needed.</p></header>'
        + "".join(manuscript)
        + ('' if manuscript else '<p>Full manuscript text is not stored in this legacy report. Saved evidence is retained in the appendix.</p>')
        + '<section class="source-legend"><h2>Source legend</h2><table><thead><tr><th>No.</th><th>Comparison paper</th><th>Status / overlap</th><th>Source pages</th></tr></thead><tbody>'
        + "".join(legend) + '</tbody></table></section>'
        + '<section class="print-evidence-appendix"><h1>Optional evidence appendix</h1>' + "".join(appendix) + '</section>'
        + '<details class="technical-details"><summary>Saved settings, methodology and complete evidence</summary><pre>' + _json(report) + '</pre></details></main>'
        + f'<script id="buna-report-data" type="application/json">{encoded}</script>'
        + (f'<script type="module">{js}</script>' if js else '')
        + '</body></html>'
    )
