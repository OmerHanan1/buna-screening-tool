import { useEffect, useState } from "react";
import { api, isActive, json } from "./api";
import PdfReport from "./PdfReport";
import LibraryPanel from "./LibraryPanel";
import type { Job, Paper } from "./types";
import type { ReaderReport } from "./reader-types";
import type { CollectionList, LibraryCollection } from "./collections";

interface LocalPaper extends Paper { filename?: string; parsed?: boolean; upload_failed?: boolean; excluded?: boolean; exclusion_reason?: string; library_paper_id?: string }
export interface LocalJob extends Job { workflow: string; filename?: string; papers: LocalPaper[] }

export default function ManualApp() {
  const [job, setJob] = useState<LocalJob | null>(null);
  const [report, setReport] = useState<ReaderReport | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [saved, setSaved] = useState<LocalJob[]>([]);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [excludeQuotes, setExcludeQuotes] = useState(true);
  const [comparisonModel, setComparisonModel] = useState("validated-lexical");
  const [engineVersion, setEngineVersion] = useState("");
  const [libraryEnabled, setLibraryEnabled] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [showFiles, setShowFiles] = useState(8);
  const [editSource, setEditSource] = useState("");
  const [doi, setDoi] = useState("");
  const [reason, setReason] = useState("");
  const [collections, setCollections] = useState<LibraryCollection[]>([]);
  const [collectionId, setCollectionId] = useState("");
  const [collectionPapers, setCollectionPapers] = useState<string[]>([]);
  const [collectionNotice, setCollectionNotice] = useState("");
  const [collectionLoading, setCollectionLoading] = useState(true);
  const [newComparison, setNewComparison] = useState(0);
  const collection = collections.find(item => item.id === collectionId);
  const active = Boolean(job && isActive(job.status));
  const locked = active || Boolean(busy);
  const ready = job?.papers.filter(p => p.parsed && !p.upload_failed && !p.excluded).length || 0;

  useEffect(() => {
    const controller = new AbortController();
    api<{ algorithm_version: string; features?: { library?: boolean } }>("/health", { signal: controller.signal })
      .then(value => {
        setEngineVersion(value.algorithm_version); setLibraryEnabled(Boolean(value.features?.library));
        if (!value.features?.library) setCollectionLoading(false);
      })
      .catch(e => { if (!controller.signal.aborted) { setError(`Server unavailable: ${(e as Error).message}`); setCollectionLoading(false); } });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    if (!libraryEnabled) return;
    const controller = new AbortController();
    setCollectionLoading(true);
    api<CollectionList>("/library/collections", { signal: controller.signal }).then(value => {
      if (controller.signal.aborted) return;
      setCollections(value.collections);
      const selected = value.collections.find(item => item.id === value.default_collection_id);
      setCollectionId(selected?.id || "");
      setCollectionPapers(selected?.ready_papers.map(paper => paper.id) || []);
    }).catch(e => { if (!controller.signal.aborted) setError(`Collection defaults unavailable: ${(e as Error).message}`); })
      .finally(() => { if (!controller.signal.aborted) setCollectionLoading(false); });
    return () => controller.abort();
  }, [libraryEnabled, newComparison]);
  useEffect(() => {
    if (!job || !isActive(job.status)) return;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      api<LocalJob>(`/jobs/${job.id}`, { signal: controller.signal }).then(setJob)
        .catch(e => { if (!controller.signal.aborted) setError(`Could not update progress: ${(e as Error).message}`); });
    }, 650);
    return () => { controller.abort(); clearInterval(timer); };
  }, [job?.id, job?.status]);
  useEffect(() => {
    if (!job?.report_available) return;
    const controller = new AbortController();
    api<ReaderReport>(`/jobs/${job.id}/report`, { signal: controller.signal }).then(setReport)
      .catch(e => { if (!controller.signal.aborted) setError((e as Error).message); });
    return () => controller.abort();
  }, [job?.id, job?.report_available, job?.updated_at]);

  async function perform(message: string, action: () => Promise<void>) {
    setBusy(message); setError("");
    try { await action(); } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  }
  function reset() {
    setJob(null); setReport(null); setError(""); setHistoryOpen(false); setEditSource("");
    setExcludeQuotes(true); setComparisonModel("validated-lexical"); setShowFiles(8);
    setCollectionNotice(""); setCollectionPapers([]); setCollectionId(""); setNewComparison(value => value + 1);
  }
  async function uploadTarget(file: File) {
    await perform("Reading your paper…", async () => {
      const body = new FormData(); body.append("file", file); body.append("manual", "true");
      const created = await api<LocalJob>("/jobs", { method: "POST", body });
      setJob(created); setReport(null);
      if (collection && collectionPapers.length) {
        const result = await api<{ job: LocalJob; skipped: { id: string; reason: string }[] }>(
          `/library/collections/${collection.id}/comparisons/${created.id}/sources`,
          json("POST", { paper_ids: collectionPapers }));
        setJob(result.job);
        setCollectionNotice(result.skipped.length ? `${result.skipped.length} identical manuscript / duplicate copies skipped.` : "");
      }
    });
  }
  async function uploadSources(files: File[]) {
    if (!job) return;
    await perform("Reading comparison papers…", async () => {
      for (let i = 0; i < files.length; i++) {
        setBusy(`Reading comparison paper ${i + 1} of ${files.length}…`);
        const body = new FormData(); body.append("file", files[i]);
        setJob(await api<LocalJob>(`/jobs/${job.id}/sources`, { method: "POST", body }));
      }
    });
  }
  const stage = job?.stage === "comparison" ? "Comparing passages" : job?.stage === "report" ? "Preparing your report" : "Preparing your papers";
  return <main className={`buna-shell ${report ? "has-report" : ""}`}>
    <header className="app-header"><a className="buna-brand" href="#" onClick={event => { event.preventDefault(); if (!locked) reset(); }}>Paper Overlap Detector</a>
      <nav><button disabled={locked} onClick={() => void perform("Opening saved comparisons…", async () => {
        setSaved(await api<LocalJob[]>("/jobs?workflow=manual")); setHistoryOpen(value => !value);
      })}>Saved comparisons</button>
        {libraryEnabled && <button onClick={() => setLibraryOpen(value => !value)}>Paper library</button>}
        {job && <button disabled={locked} onClick={reset}>New comparison</button>}</nav>
    </header>
    {historyOpen && <section className="saved-panel" aria-label="Saved comparisons"><div className="section-heading"><h2>Saved comparisons</h2>
      <button onClick={() => setHistoryOpen(false)} aria-label="Close saved comparisons">×</button></div>
      <p className="muted">Opening a report does not rerun it. Starting a new comparison keeps previous files.</p>
      {!saved.length && <p>No saved manual comparisons yet.</p>}
      {saved.slice(0, 30).map(item => <button className="saved-row" key={item.id} disabled={locked} onClick={() => void perform("Opening report…", async () => {
        const value = await api<LocalJob>(`/jobs/${item.id}`);
        if (value.workflow !== "manual") throw new Error("This is not a manual comparison.");
        setReport(null); setJob(value); setHistoryOpen(false); setCollectionId(""); setCollectionNotice("");
      })}><span>{item.filename || item.title}</span><small>{new Date(item.created_at).toLocaleDateString()} · {item.status}</small></button>)}
      {saved.length > 30 && <p className="muted">Showing the 30 most recent comparisons.</p>}
    </section>}
    {error && <div className="app-error" role="alert">{error}</div>}
    {libraryEnabled && libraryOpen && <LibraryPanel job={job} locked={locked} onAttached={setJob} onClose={() => setLibraryOpen(false)} />}
    {report && job ? <PdfReport report={report} jobId={job.id} engineVersion={engineVersion} /> :
      <div className="setup">
        <header className="setup-intro"><p className="eyebrow">LOCAL TEXT COMPARISON</p>
          <h1>Your paper, in context.</h1><p>Compare one manuscript with the papers you choose.</p></header>
        <div className="upload-grid">
          <section className="upload-section"><div className="section-heading"><h2>Your paper</h2><span>01</span></div>
            {job ? <div className="target-ready"><span className="file-symbol" aria-hidden="true">▤</span><strong>{job.filename || job.title}</strong>
              <small>Text extracted · Ready</small></div> :
              <label className="upload-zone"><span className="file-symbol" aria-hidden="true">↑</span><strong>Choose your manuscript</strong>
                <span>One PDF or text file</span><input aria-label="Your paper" type="file" accept=".pdf,.txt" disabled={locked || collectionLoading}
                  onChange={event => { const file = event.target.files?.[0]; if (file) void uploadTarget(file); event.target.value = ""; }} /></label>}
          </section>
          <section className="upload-section"><div className="section-heading"><h2>Comparison papers</h2><span>{job?.papers.length || "02"}</span></div>
            {libraryEnabled && !job && collections.length > 0 && <div className="library-local">
              <label>Default collection<select aria-label="Default collection" value={collectionId} disabled={locked}
                onChange={event => void perform("Saving collection preference…", async () => {
                  const id = event.target.value;
                  await api("/library/collections/default", json("PUT", { collection_id: id || null }));
                  setCollectionId(id);
                  setCollectionPapers(collections.find(item => item.id === id)?.ready_papers.map(paper => paper.id) || []);
                })}><option value="">None — choose papers myself</option>
                {collections.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
              {collection && <>
                <p role="status">{collection.name}: {collectionPapers.length} selected</p>
                <p className="muted">Ready {collection.ready_references} / {collection.total_references} reference entries · {collection.ready_unique} distinct papers.
                  Selection is a snapshot for this new comparison. Nothing runs automatically.</p>
                <details><summary>Change selected collection papers</summary>
                  <button disabled={locked} onClick={() => setCollectionPapers([])}>Deselect all</button>
                  {collection.ready_papers.map(paper => <label className="library-choice" key={paper.id}>
                    <input type="checkbox" aria-label={`Include ${paper.title} in collection selection`} checked={collectionPapers.includes(paper.id)} disabled={locked}
                      onChange={event => setCollectionPapers(current => event.target.checked ? [...current, paper.id] : current.filter(id => id !== paper.id))} />
                    <span>{paper.title}<small>{paper.version}</small></span>
                  </label>)}
                </details>
              </>}
            </div>}
            {job && collection && <p role="status">{collection.name}: {job.papers.filter(p =>
              !p.excluded && collection.ready_papers.some(member => member.id === p.library_paper_id)).length} selected
              {collectionNotice && ` · ${collectionNotice}`}. Remove any source below to opt out.</p>}
            <label className={`upload-zone source-upload ${job ? "" : "is-disabled"}`}><span className="file-symbol" aria-hidden="true">+</span><strong>Add comparison papers</strong>
              <span>{job ? "Multiple PDF or text files" : "Choose your manuscript first"}</span>
              <input aria-label="Comparison papers" type="file" accept=".pdf,.txt" multiple disabled={!job || locked || Boolean(job.report_available)}
                onChange={event => { const files = Array.from(event.target.files || []); if (files.length) void uploadSources(files); event.target.value = ""; }} /></label>
            <ul className="setup-files">{job?.papers.slice(0, showFiles).map(paper => <li key={paper.id}>
              <div><strong>{paper.filename || paper.title}</strong><small className={paper.upload_failed ? "error-text" : ""} role={paper.upload_failed ? "alert" : undefined}>
                {paper.excluded ? `Excluded · ${paper.exclusion_reason}` : paper.error || (paper.parsed ? "Ready" : "Not readable")}</small></div>
              {libraryEnabled && paper.parsed && <button disabled={locked} onClick={() => void perform("Saving paper to library…", async () => {
                await api(`/library/comparisons/${job.id}/sources/${paper.id}/save`, { method: "POST" });
                setLibraryOpen(true);
              })}>Save to library</button>}
              {!job.report_available && <button aria-label={`Remove ${paper.filename || paper.title}`} disabled={locked} onClick={() => void perform("Updating files…", async () => {
                setJob(await api<LocalJob>(`/jobs/${job.id}/sources/${paper.id}`, { method: "DELETE" }));
              })}>×</button>}
            </li>)}</ul>
            {(job?.papers.length || 0) > showFiles && <button onClick={() => setShowFiles(value => value + 20)}>Show more files</button>}
          </section>
        </div>
        <div className="setup-action"><p>{libraryEnabled ? "Comparison runs locally using your uploaded and selected library papers. DOI import is separate." : "Files stay on this computer. No online search or external processing."}</p>
          {!active && <button className="primary" disabled={!job || !ready || locked || job.report_available} onClick={() => void perform("Starting comparison…", async () => {
            if (job) setJob(await api<LocalJob>(`/jobs/${job.id}/run`, json("POST", {
              mode: "offline", exclude_quotes: excludeQuotes, comparison_model: comparisonModel,
            })));
          })}>Compare papers{ready ? ` · ${ready}` : ""}</button>}
        </div>
        {busy && <p role="status" className="setup-progress">{busy}</p>}
        {active && <section className="progress-panel" aria-live="polite"><div className="section-heading"><h2>{stage}</h2>
          <button disabled={Boolean(busy)} onClick={() => void perform("Cancelling…", async () => {
            if (job) setJob(await api<LocalJob>(`/jobs/${job.id}/cancel`, { method: "POST" }));
          })}>Cancel</button></div><progress max={100} value={job?.progress || 0} aria-label="Comparison progress" />
          <p>{job?.message}</p></section>}
        {job && ["failed", "cancelled"].includes(job.status) && <div role="alert" className="app-error">{job.message}</div>}
        {job?.report_available && !report && <p role="status">Opening your saved report…</p>}
        <details className="setup-advanced"><summary>Advanced settings</summary>
          <label><input type="checkbox" checked={excludeQuotes} disabled={locked} onChange={event => setExcludeQuotes(event.target.checked)} /> Exclude recognized quotations from matching</label>
          <label>Comparison model <select value={comparisonModel} disabled={locked} onChange={event => setComparisonModel(event.target.value)}>
            <option value="validated-lexical">Standard wording comparison</option>
            <option value="experimental-ordered">Experimental ordered-instance model</option>
            <option value="improvedEng">improvedEng · ordered lexical matching (experimental)</option>
          </select></label>
          {comparisonModel !== "validated-lexical" && <p className="app-error">Experimental: increased recall has not established precision. This is not a validated vendor-equivalent model.</p>}
          {comparisonModel === "improvedEng" && <p>Exact matching unchanged. Similar: four-content-word exact anchor, at least nine qualifying matches, 70% local agreement and cumulative gap limits. Citations cannot qualify a Similar match. Excluded text cannot connect passages; partial coverage cannot calibrate recall.</p>}
          <p className="muted">Algorithm {engineVersion || "unavailable"} · Nine-word configured minimum · Eligible manuscript words after exclusions.
            Manuscript limits: 20 MiB, 250 pages, 1 million extracted characters. Comparison sources: 32 MiB, 600 pages, 2 million characters. Matching remains bounded to 120 seconds/source and 64 MiB estimated retained evidence; large sources may be partially checked.
            Scanned PDFs need OCR before upload.</p>
          {job?.document?.warnings && <details><summary>Extraction notes</summary><ul>{job.document.warnings.map((note, i) => <li key={i}>{note}</li>)}</ul></details>}
          {job && Boolean(job.papers.length) && <div className="source-settings">
            <label>Source metadata / exclusion<select aria-label="Edit source metadata" value={editSource} onChange={event => {
              const source = job.papers.find(p => p.id === event.target.value); setEditSource(event.target.value);
              setDoi(source?.doi || ""); setReason(source?.exclusion_reason || "");
            }}><option value="">Choose a source</option>{job.papers.map(paper => <option key={paper.id} value={paper.id}>{paper.filename || paper.title}</option>)}</select></label>
            {editSource && <><label>Optional DOI<input value={doi} onChange={event => setDoi(event.target.value)} /></label>
              <label>Exclusion reason<input value={reason} onChange={event => setReason(event.target.value)} /></label>
              <div className="reader-actions">{[false, true].map(excluded => <button key={String(excluded)} disabled={locked || (excluded && !reason.trim())}
                onClick={() => void perform("Saving source settings…", async () => {
                  setJob(await api<LocalJob>(`/jobs/${job.id}/sources/${editSource}`, json("PATCH", { excluded, reason, doi })));
                })}>{excluded ? "Exclude source" : "Include / save DOI"}</button>)}</div></>}
          </div>}
        </details>
      </div>}
    <footer className="app-footer">Paper Overlap Detector</footer>
  </main>;
}
