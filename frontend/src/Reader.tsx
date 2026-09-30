import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "./api";
import { highlightedFragments } from "./evidence";
import type { Segment } from "./types";
import type { ReaderFragment, ReaderReport, ReaderSource } from "./reader-types";

export function statusLabel(status: string): string {
  if (status === "compared") return "Checked";
  if (status === "compared-with-limits") return "Partly checked";
  if (status.startsWith("excluded")) return "Excluded";
  if (["parsed", "discovered", "pending"].includes(status)) return "Not checked";
  return "Unavailable";
}

function Excerpt({ segment }: { segment: Segment }) {
  return <div className="reader-excerpt">{highlightedFragments(segment.text, segment.highlights).map((part, index) =>
    part.highlighted ? <mark key={index}>{part.text}</mark> : <span key={index}>{part.text}</span>)}</div>;
}

function LocalSource({ jobId, source }: { jobId: string; source: ReaderSource }) {
  const [value, setValue] = useState<{ pages: { number: number; text: string; highlights: [number, number][] }[] }>();
  const [index, setIndex] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  return <details className="reader-local-source" onToggle={event => {
    if (event.currentTarget.open && !value && !busy && !error) {
      setBusy(true);
      api<NonNullable<typeof value>>(`/jobs/${jobId}/sources/${source.id}/text`).then(setValue)
        .catch(e => setError((e as Error).message)).finally(() => setBusy(false));
    }
  }}><summary>Open full local source text</summary>
    {busy && <p role="status">Loading source…</p>}{error && <p role="alert">{error}</p>}
    {value && <><div className="reader-navigation"><button disabled={!index} onClick={() => setIndex(i => i - 1)}>Previous</button>
      <span>Source page {value.pages[index].number}</span><button disabled={index + 1 >= value.pages.length} onClick={() => setIndex(i => i + 1)}>Next</button></div>
      <Excerpt segment={value.pages[index]} /></>}
  </details>;
}

export default function Reader({ report, jobId, engineVersion, standalone = false }: {
  report: ReaderReport; jobId?: string; engineVersion?: string; standalone?: boolean;
}) {
  const [pageIndex, setPageIndex] = useState(0);
  const [sourceFilter, setSourceFilter] = useState("all");
  const [showExcluded, setShowExcluded] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const [options, setOptions] = useState<number[]>([]);
  const [query, setQuery] = useState("");
  const [sourcePage, setSourcePage] = useState(0);
  const [paneOpen, setPaneOpen] = useState(false);
  const excerptHeading = useRef<HTMLHeadingElement>(null);
  const lastHighlight = useRef<HTMLElement | null>(null);
  const sourceList = useRef<HTMLDivElement>(null);
  const paneToggle = useRef<HTMLButtonElement>(null);
  const { pages, sources, groups } = report.reader;
  const matchPriority = useMemo(() => new Map(groups.flatMap(group => group.match_indices).map((index, rank) => [index, rank])), [groups]);
  const page = pages[pageIndex];
  const sourceMap = useMemo(() => new Map(sources.map(source => [source.id, source])), [sources]);
  const visible = (indices: number[]) => indices.filter(index =>
    (showExcluded || !report.matches[index].excluded_from_score) &&
    (sourceFilter === "all" || report.matches[index].source_id === sourceFilter));
  const navigable = groups.map(group => ({ group, indices: visible(group.match_indices) })).filter(item => item.indices.length);
  const navIndex = navigable.findIndex(item => selected !== null && item.group.match_indices.includes(selected));
  const match = selected === null ? null : report.matches[selected];
  const selectedSource = match ? sourceMap.get(match.source_id) : sourceMap.get(sourceFilter);
  const filteredSources = sources.filter(source => `${source.number} ${source.title} ${statusLabel(source.status)}`.toLowerCase().includes(query.toLowerCase()));
  const result = report.result;
  const denominator = report.metrics.score_denominator_words ?? report.metrics.eligible_words;
  const count = groups.filter(group => group.match_indices.some(i => !report.matches[i].excluded_from_score)).length;

  useEffect(() => {
    if (selected !== null) excerptHeading.current?.focus({ preventScroll: true });
  }, [selected]);

  function choose(index: number, alternatives?: number[], openPane = false, keepPage = false) {
    const current = report.matches[index];
    setSelected(index);
    setOptions(alternatives || groups.find(group => group.match_indices.includes(index))?.match_indices || [index]);
    const start = current.manuscript.match_start ?? current.manuscript.start ?? 0;
    const found = pages.findIndex(item => item.start <= start && start < item.start + Array.from(item.text).length);
    if (found >= 0 && !keepPage) setPageIndex(found);
    if (openPane) {
      setPaneOpen(true); setQuery("");
      setSourcePage(Math.max(0, Math.floor(sources.findIndex(source => source.id === current.source_id) / 15)));
      requestAnimationFrame(() => { if (sourceList.current) sourceList.current.scrollTop = 0; });
    }
  }
  function filterSource(id: string) {
    setSourceFilter(id); setSelected(null); setOptions([]);
    if (id === "all") {
      setSourcePage(0);
      if (sourceList.current) sourceList.current.scrollTop = 0;
    }
    const next = groups.flatMap(group => group.match_indices).find(index =>
      (id === "all" || report.matches[index].source_id === id) &&
      (showExcluded || !report.matches[index].excluded_from_score));
    if (next !== undefined) choose(next);
  }
  function closePane() {
    setPaneOpen(false);
    (lastHighlight.current?.isConnected ? lastHighlight.current : paneToggle.current)?.focus({ preventScroll: true });
  }
  function fragment(fragment: ReaderFragment, key: number, printable = false) {
    const active = [...(printable ? fragment.match_indices : visible([...fragment.match_indices, ...(showExcluded ? fragment.excluded_indices : [])]))]
      .sort((a, b) => (matchPriority.get(a) ?? a) - (matchPriority.get(b) ?? b));
    if (!active.length) return <span key={key}>{fragment.text}</span>;
    const ids = [...new Set(active.map(index => sourceMap.get(report.matches[index].source_id)?.number).filter((value): value is number => value !== undefined))].sort((a, b) => a - b);
    const excluded = active.every(index => report.matches[index].excluded_from_score);
    const label = `Sources ${ids.join(", ")}${excluded ? "; excluded evidence" : ""}`;
    const reference = <sup>{ids.slice(0, 3).join(",")}{ids.length > 3 ? ` +${ids.length - 3}` : ""}</sup>;
    if (printable) return <span key={key}><mark>{fragment.text}</mark>{reference}</span>;
    return <span key={key} role="button" tabIndex={0} className={`reader-highlight ${selected !== null && active.includes(selected) ? "is-selected" : ""} ${excluded ? "is-excluded" : ""}`}
      aria-label={label} title={label} aria-pressed={selected !== null && active.includes(selected)}
      onClick={event => { lastHighlight.current = event.currentTarget; choose(active[0], active, true, true); }}
      onKeyDown={event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault(); lastHighlight.current = event.currentTarget; choose(active[0], active, true, true);
        }
      }}>{fragment.text}{reference}</span>;
  }
  function print(appendix = false) {
    document.body.classList.toggle("include-evidence", appendix);
    window.print();
  }
  return <section className={`reader ${standalone ? "reader-standalone" : ""}`} aria-label="Manuscript review">
    <header className="reader-summary">
      <div><p className="eyebrow">MANUSCRIPT REVIEW</p><h1>{report.job?.title || "Your paper"}</h1>
        <p className="reader-note">Text overlap for human review · Uploaded sources only</p></div>
      <div className="reader-score"><strong>{result.score_available ? `${report.metrics.overlap_percent}%` : "—"}</strong>
        <span>{result.score_kind === "lower_bound" ? "overlap · lower bound" : "text overlap"}</span></div>
      <div className="reader-counts"><strong>{result.complete_sources}/{sources.length}</strong><span>papers fully checked</span>
        <small>{count} highlighted passages</small></div>
      {standalone ? <button className="primary" onClick={() => print()}>Print annotated manuscript</button> :
        <a className="primary button" href={`/api/jobs/${jobId}/report.html`} download>Download report</a>}
    </header>
    {result.state !== "complete" && <div className="reader-notice" role="status"><strong>{result.heading}</strong>
      <span>{result.explanation}</span><small>{result.partial_sources} partly checked · {result.unchecked_sources} unavailable · {result.excluded_sources} excluded</small></div>}
    {report.comparison_model === "experimental-ordered" && <div className="reader-notice">Experimental model: precision and proprietary equivalence are not established.</div>}
    {report.comparison_model === "improvedEng" && <div className="reader-notice">improvedEng: experimental ordered lexical matching. No semantic matching or verified Crossref equivalence.</div>}
    <details className="reader-details"><summary>Report details</summary>
      <div className="reader-detail-grid"><p>Saved algorithm: {report.algorithm_version || "Legacy"}{engineVersion ? ` · Running engine: ${engineVersion}` : ""}<br />
        Generated: {report.generated_at ? new Date(report.generated_at).toLocaleString() : "Not recorded"}</p>
        <p>{report.metrics.overlapping_words} matching words / {denominator} {report.metrics.score_basis === "abstract-onward-word-units" ? "words from the Abstract onward" : report.metrics.score_basis === "all-submitted-word-units" ? "total submitted word units" : "eligible manuscript words"}.
          Word positions shared across sources count once.</p>
        <p>Bibliography: {report.metrics.bibliography_words ?? "—"} words excluded from matching. Quotes: {report.metrics.excluded_quotation_words ?? "—"} words.
          This interpretation is documented, not proprietary-score parity.</p>
        <p>{report.reader.fidelity} Source numbers identify files, not passage counts.</p></div>
      <ul>{report.warnings?.map((warning, i) => <li key={i}>{warning}</li>)}</ul>
      <details><summary>Saved methodology and filter settings</summary><pre>{JSON.stringify({ settings: report.settings, methodology: report.methodology }, null, 2)}</pre></details>
      <div className="reader-actions"><button onClick={() => print()}>Print manuscript + source legend</button>
        <button onClick={() => print(true)}>Print with evidence appendix</button>
        {!standalone && <a href={`/api/jobs/${jobId}/report.json`} download>Download JSON</a>}
        {standalone && <button onClick={() => {
          const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }));
          const anchor = document.createElement("a"); anchor.href = url; anchor.download = "paper-overlap-report.json"; anchor.click(); URL.revokeObjectURL(url);
        }}>Download JSON</button>}</div>
    </details>
    <div className="reader-workspace">
      <div className="reader-document">
        <div className="reader-toolbar"><strong>Your paper</strong>
          <div className="reader-navigation"><button aria-label="Previous manuscript page" disabled={!pageIndex} onClick={() => { setPageIndex(value => value - 1); setSelected(null); }}>←</button>
            <label>Page <select aria-label="Manuscript page" value={pageIndex} onChange={event => { setPageIndex(Number(event.target.value)); setSelected(null); }}>
              {pages.map((item, index) => <option key={index} value={index}>{item.number}</option>)}</select> / {pages.length}</label>
            <button aria-label="Next manuscript page" disabled={pageIndex + 1 >= pages.length} onClick={() => { setPageIndex(value => value + 1); setSelected(null); }}>→</button></div>
          <button ref={paneToggle} className="reader-pane-toggle" onClick={() => setPaneOpen(!paneOpen)} aria-expanded={paneOpen}>Sources</button></div>
        <div className="reader-reading-note">Extracted text · Original page correspondence retained</div>
        <article className="reader-paper" aria-label={`Manuscript page ${page?.number ?? "unavailable"}`}>
          {page ? <><div className="reader-page-label">PAGE {page.number}</div>
            <div className="reader-paper-text">{page.fragments.map((item, i) => fragment(item, i))}</div></> :
            <p>This legacy report did not save its full manuscript. Source excerpts remain available.</p>}
        </article>
      </div>
      <aside className={`reader-pane ${paneOpen ? "is-open" : ""}`} aria-label="Sources and evidence" onKeyDown={event => {
        if (event.key === "Escape" && paneOpen) closePane();
      }}>
        <div className="reader-pane-head"><h2>Comparison papers <span>{sources.length}</span></h2>
          <button className="reader-pane-toggle" onClick={closePane} aria-label="Close source pane">×</button></div>
        <label className="reader-search"><span className="sr-only">Find a source</span><input aria-label="Find a source" placeholder="Find a source…" value={query}
          onChange={event => { setQuery(event.target.value); setSourcePage(0); }} /></label>
        <div className="reader-filter-row"><button aria-pressed={sourceFilter === "all"} onClick={() => filterSource("all")}>All sources</button>
          <label><input type="checkbox" checked={showExcluded} onChange={event => { setShowExcluded(event.target.checked); setSelected(null); }} /> Include excluded evidence</label></div>
        <div className="reader-source-list" ref={sourceList}>
          {filteredSources.slice(sourcePage * 15, (sourcePage + 1) * 15).map(source => <button key={source.id} className={`reader-source ${sourceFilter === source.id ? "is-active" : ""} ${match?.source_id === source.id ? "is-focused" : ""}`}
            aria-pressed={sourceFilter === source.id} aria-label={`Source ${source.number}: ${source.title}`} onClick={() => filterSource(source.id)}>
            <span className="reader-source-number">{source.number}</span><span className="reader-source-name">{source.title}
              <small>{statusLabel(source.status)}{source.reason ? ` · ${source.reason}` : ""}</small></span>
            <span className="reader-source-score">{["compared", "compared-with-limits"].includes(source.status) && typeof source.overlap_percent === "number" ? `${source.overlap_percent}%` : "—"}</span></button>)}
        </div>
        {filteredSources.length > 15 && <div className="reader-navigation"><button disabled={!sourcePage} onClick={() => setSourcePage(value => value - 1)}>Previous sources</button>
          <small>{sourcePage * 15 + 1}–{Math.min(filteredSources.length, (sourcePage + 1) * 15)} of {filteredSources.length}</small>
          <button disabled={(sourcePage + 1) * 15 >= filteredSources.length} onClick={() => setSourcePage(value => value + 1)}>Next sources</button></div>}
        <div className="reader-evidence">
          <div className="reader-navigation"><button aria-label="Previous matched passage" disabled={navIndex <= 0} onClick={() => choose(navigable[navIndex - 1].indices[0])}>←</button>
            <span>{navigable.length ? `Passage ${navIndex >= 0 ? navIndex + 1 : "—"} of ${navigable.length}` : "No linked passages"}</span>
            <button aria-label="Next matched passage" disabled={!navigable.length || navIndex + 1 >= navigable.length} onClick={() => choose(navigable[Math.max(0, navIndex + 1)].indices[0])}>→</button></div>
          {match && selectedSource ? <><h3 ref={excerptHeading} tabIndex={-1}>Source {selectedSource.number} · {selectedSource.title}</h3>
            <p className="reader-note">Source page {match.source.pages?.join(", ") || match.source.page || "unknown"} · {match.kind === "exact" ? "Exact wording" : "Near-identical wording"}</p>
            {options.length > 1 && <label className="reader-location">Matching sources / locations<select aria-label="Matching source location" value={selected ?? ""} onChange={event => choose(Number(event.target.value), options, false, true)}>
              {options.map(index => {
                const item = report.matches[index]; const source = sourceMap.get(item.source_id);
                return <option key={index} value={index}>Source {source?.number} · page {item.source.page ?? "?"} · location {index + 1}</option>;
              })}</select></label>}
            <Excerpt segment={match.source} />
            <p className="reader-classification">{match.classification || "Text overlap for review"}</p>
            {match.excluded_from_score && <p className="reader-excluded-note">Excluded from the score: {match.exclusion_reasons?.join(", ") || "saved exclusion"}.</p>}
            <details><summary>Passage context and attribution</summary><p>{match.citation?.basis}</p>
              <p>{match.included_words ?? "—"} words included · Quotation: {match.quotation?.status || "not recorded"}</p>
              <h4>Your original passage</h4><Excerpt segment={match.manuscript} /></details>
            {!standalone && jobId && <LocalSource key={selectedSource.id} jobId={jobId} source={selectedSource} />}
          </> : <div className="reader-empty"><strong>{selectedSource ? statusLabel(selectedSource.status) : "Read in context"}</strong>
            <p>{selectedSource?.reason || (sourceFilter !== "all" ? "No included matching passages for this source. Its status remains in the source list." :
              "Select highlighted words in your paper to inspect the source. Source numbers stay the same throughout the report.")}</p>
            {!standalone && jobId && selectedSource && ["compared", "compared-with-limits"].includes(selectedSource.status) &&
              <LocalSource key={selectedSource.id} jobId={jobId} source={selectedSource} />}</div>}
        </div>
      </aside>
    </div>
    {!standalone && <div className="reader-print-copy">
      <h1>{report.job?.title || "Manuscript comparison"}</h1><p>{result.heading}. {result.complete_sources}/{sources.length} papers fully checked.
        {result.score_available ? ` ${report.metrics.overlap_percent}% overlap; ${report.metrics.overlapping_words}/${denominator} words.` : ""}</p>
      <p>Text overlap, not a plagiarism verdict. Page-preserving extracted text; source numbers refer to the legend.</p>
      {pages.map(item => <article className="print-manuscript-page" key={item.number}><h2>Manuscript page {item.number}</h2>
        <div className="reader-paper-text">{item.fragments.map((part, i) => fragment(part, i, true))}</div></article>)}
      <h2>Source legend</h2><ol>{sources.map(source => <li key={source.id} value={source.number}><strong>{source.title}</strong> — {statusLabel(source.status)}
        {["compared", "compared-with-limits"].includes(source.status) && typeof source.overlap_percent === "number" ? `, ${source.overlap_percent}%` : ""}. {source.reason}</li>)}</ol>
      <div className="print-evidence-appendix"><h2>Optional evidence appendix</h2>{report.matches.map((item, index) =>
        <article key={index}><h3>Source {sourceMap.get(item.source_id)?.number} · manuscript page {item.manuscript.page} / source page {item.source.page}</h3>
          <Excerpt segment={item.manuscript} /><Excerpt segment={item.source} /></article>)}</div>
    </div>}
  </section>;
}
