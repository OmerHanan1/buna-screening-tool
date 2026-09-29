import { useEffect, useRef, useState } from "react";
import { ArrowRight, Download, ExternalLink, FileText, LoaderCircle, Plus, X } from "lucide-react";
import { Elapsed, fileSize, ManuscriptInput, PaperDialog, validateFile, type HostedPaper } from "./PublicControls";
import "./public.css";

type Job = { id: string; status: string; checked?: number; total?: number; overlap_percent?: number; score_available?: boolean; partial?: boolean; warnings?: string[]; error?: string; algorithm_version?: string; error_code?: string; diagnostic_id?: string; evidence_available?: boolean; progress?: { stage?: string; source_index?: number; source_count?: number; checked_sources?: number; elapsed_seconds?: number } };
const base = (import.meta.env.VITE_PUBLIC_API_URL || "").replace(/\/$/, "");
const gatedMode = import.meta.env.VITE_EMAIL_GATE === "true";

export default function PublicApp() {
  const [papers, setPapers] = useState<HostedPaper[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [target, setTarget] = useState<File | null>(null);
  const [sources, setSources] = useState<File[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [retryStatus, setRetryStatus] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [consent, setConsent] = useState(false);
  const [loadingLibrary, setLoadingLibrary] = useState(true);
  const [entered, setEntered] = useState(false);
  const [email, setEmail] = useState("");
  const [gateReady, setGateReady] = useState(false);
  const [entering, setEntering] = useState(false);
  const [fileError, setFileError] = useState("");
  const [sourceError, setSourceError] = useState("");
  const [reviewing, setReviewing] = useState(false);
  const [pdfBusy, setPdfBusy] = useState(false);
  const [startedAt, setStartedAt] = useState(0);
  const [submitted, setSubmitted] = useState({ title: "", count: 0 });
  const extraInput = useRef<HTMLInputElement>(null);
  const token = useRef("");
  const urls = useRef<string[]>([]);
  const targetChoice = useRef(0);
  const submission = useRef<{ key: string; fingerprint: string } | null>(null);

  async function request(path: string, options: RequestInit = {}) {
    const response = await fetch(`${base}/api/public${path}`, {
      ...options, signal: AbortSignal.timeout(90000), credentials: "omit",
      headers: { ...options.headers, ...(token.current ? { Authorization: `Bearer ${token.current}` } : {}) },
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) {
        setEntered(false); token.current = ""; setJob(null); setPapers([]); setSelected([]);
        throw new Error("This workspace expired or the service restarted. Enter your email to start a new one.");
      }
      if (response.status === 404 && path.startsWith("/jobs/")) setJob(null);
      throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status}).`);
    }
    return response;
  }
  async function loadLibrary() {
    setLoadingLibrary(true); setError("");
    try {
      const response = gatedMode ? await request("/library") : await fetch(`${base}/api/public/library`, { credentials: "omit", signal: AbortSignal.timeout(90000) });
      if (!response.ok) throw new Error("The comparison library could not be loaded. Please retry.");
      const data = await response.json();
      setPapers(data.papers); setSelected(data.papers.map((paper: HostedPaper) => paper.sha256));
    } catch (e) { setError(e instanceof Error ? e.message : "The service could not be reached. Retry shortly."); }
    finally { setLoadingLibrary(false); }
  }
  useEffect(() => {
    sessionStorage.removeItem("paper-overlap-public-session");
    sessionStorage.removeItem("paper-overlap-public-job");
    if (gatedMode) {
      void (async () => {
        const response = await fetch(`${base}/api/auth/config`, { credentials: "omit", signal: AbortSignal.timeout(90000) });
        if (!response.ok) throw new Error("The service could not be reached. Retry shortly.");
        if ((await response.json()).mode !== "email-gate") throw new Error("The email access gate is not available yet.");
        setGateReady(true); setLoadingLibrary(false);
      })().catch(e => { setError(e.message); setLoadingLibrary(false); });
    } else void loadLibrary();
    return () => { urls.current.forEach(URL.revokeObjectURL); };
  }, []);
  useEffect(() => {
    if (!job || job.status !== "running" || (gatedMode && !entered)) return;
    let stopped = false, failures = 0, timer = 0;
    async function poll() {
      try {
        const result = await (await request(`/jobs/${job!.id}`)).json();
        if (!stopped) { setJob(result); setError(""); failures = 0; }
      } catch (e) { if (!stopped) { setError((e as Error).message); failures++; } }
      if (!stopped && failures < 3) timer = window.setTimeout(poll, 3000);
    }
    timer = window.setTimeout(poll, 1000);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [job?.id, job?.status, retryStatus, entered]);

  async function enter() {
    if (!gateReady || entering) return;
    setError(""); setEntering(true);
    try {
      const data = await (await request("/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }) })).json();
      token.current = data.token; setEmail(""); setEntered(true); await loadLibrary();
    } catch (e) { setError((e as Error).message); }
    finally { setEntering(false); }
  }
  function leave() {
    if (job?.status === "running") { setError("Cancel the running comparison before leaving this workspace."); return; }
    if (job?.status === "complete" && !window.confirm("Download your report first. Leaving removes this page’s access to it. Leave workspace?")) return;
    token.current = ""; setEntered(false); urls.current.forEach(URL.revokeObjectURL); urls.current = [];
    submission.current = null;
    setJob(null); setPapers([]); setSelected([]); setTarget(null); setSources([]); setConsent(false); setError("");
  }
  async function chooseTarget(file: File) {
    if (busy) return;
    const choice = ++targetChoice.current;
    const invalid = await validateFile(file, 10);
    if (choice !== targetChoice.current) return;
    setFileError(invalid);
    if (!invalid) setTarget(file);
  }
  async function addSources(files: File[]) {
    if (busy) return;
    setSourceError("");
    if (sources.length + files.length > 5) { setSourceError("You can add up to five comparison files."); return; }
    for (const file of files) {
      const invalid = await validateFile(file, 8);
      if (invalid) { setSourceError(`${file.name}: ${invalid}`); return; }
    }
    setSources(previous => {
      const result = [...previous];
      for (const file of files) {
        if (!result.some(s => s.name === file.name && s.size === file.size && s.lastModified === file.lastModified)) result.push(file);
      }
      if (result.length > 5) { setSourceError("You can add up to five comparison files."); return previous; }
      return result;
    });
  }
  async function compare() {
    if (!target || !consent || busy) return;
    setBusy(true); setError("");
    setSubmitted({ title: target.name, count: selected.length + sources.length });
    try {
      if (!gatedMode && !token.current) token.current = (await (await request("/session", { method: "POST" })).json()).token;
      const form = new FormData();
      form.append("target", target); form.append("selected", JSON.stringify(selected));
      sources.forEach(file => form.append("sources", file));
      const fingerprint = JSON.stringify({ target: [target.name, target.size, target.lastModified], selected,
        sources: sources.map(file => [file.name, file.size, file.lastModified]) });
      if (submission.current?.fingerprint !== fingerprint) submission.current = { key: crypto.randomUUID(), fingerprint };
      const created = await (await request("/jobs", { method: "POST", body: form, headers: { "Idempotency-Key": submission.current.key } })).json();
      const current = created.status === "running" ? created : await (await request(`/jobs/${created.id}`)).json();
      setStartedAt(Date.now()); setJob(current);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function reportFile(format: "pdf" | "json", open = false) {
    if (!job || pdfBusy) return;
    const viewer = open ? window.open("about:blank", "_blank") : null;
    if (viewer) viewer.opener = null;
    setPdfBusy(true); setError("");
    try {
      const blob = await (await request(`/jobs/${job.id}/report.${format}`)).blob();
      const url = URL.createObjectURL(blob); urls.current.push(url);
      if (open) {
        if (!viewer) throw new Error("Your browser blocked the PDF tab. Use Download PDF instead.");
        viewer.location.replace(url);
      } else {
        const link = document.createElement("a"); link.href = url; link.download = `paper-overlap-report.${format}`; link.click();
      }
    } catch (e) { viewer?.close(); setError((e as Error).message); }
    finally { setPdfBusy(false); }
  }
  function credits() {
    const url = URL.createObjectURL(new Blob([JSON.stringify({ papers }, null, 2)], { type: "application/json" }));
    urls.current.push(url);
    const link = document.createElement("a"); link.href = url; link.download = "source-attributions.json"; link.click();
  }
  async function remove() {
    if (!job) return;
    try {
      const value = await (await request(`/jobs/${job.id}`, { method: "DELETE" })).json();
      setJob(value.status === "deleted" ? null : { ...job, status: value.status });
      if (value.status === "deleted") submission.current = null;
    } catch (e) { setError((e as Error).message); }
  }
  function newComparison() {
    if (!window.confirm("Keep your downloaded report before starting a new comparison. Continue?")) return;
    submission.current = null;
    setJob(null); setTarget(null); setSources([]); setConsent(false); setError(""); setFileError(""); setSourceError("");
  }
  const hasAccess = !gatedMode || entered;
  const totalUpload = (target?.size || 0) + sources.reduce((sum, file) => sum + file.size, 0);
  const disabledReason = !target ? "Add your manuscript to begin." : !selected.length && !sources.length ? "Select at least one comparison paper."
    : totalUpload > 32 * 1024 * 1024 - 16384 ? "Combined uploads exceed the 32 MB request limit."
    : !consent ? "Confirm your upload permission below." : "";
  const fallbackWarning = job?.warnings?.find(w => w.startsWith("Abstract heading not detected"));
  const stages: Record<string, string> = { starting: "Starting the isolated comparison", "parse-manuscript": "Extracting manuscript text",
    "load-source": "Loading comparison text", compare: "Checking matching passages", "write-evidence": "Saving comparison evidence",
    "render-pdf": "Preparing the annotated PDF", attribution: "Adding source credits", complete: "Finishing the report" };

  return <div className="hosted-app">
    <header className="hosted-header"><div className="hosted-brand"><FileText size={19} aria-hidden="true" />Paper Overlap Detector</div>
      {hasAccess ? <button className="hosted-text-button" onClick={leave}>Leave workspace</button> : <span className="hosted-header-note">Text similarity for research</span>}</header>
    <main className={`hosted-main ${!hasAccess ? "hosted-entry" : ""}`}>
      {!hasAccess ? <>
        <div className="hosted-title"><h1>Open your workspace</h1><p>Enter your email to continue.</p></div>
        <form onSubmit={e => { e.preventDefault(); void enter(); }}>
          <label htmlFor="access-email" className="hosted-label">Email address<input id="access-email" type="email" maxLength={254} autoComplete="email" value={email} onChange={e => setEmail(e.target.value)} placeholder="you@example.com" /></label>
          {error && <p role="alert" className="hosted-error">{error}</p>}
          <button className="hosted-primary" disabled={!gateReady || !email.trim() || entering}>{entering ? "Opening workspace…" : "Continue"}<ArrowRight size={16} aria-hidden="true" /></button>
        </form>
        {loadingLibrary && <p role="status" className="hosted-loading"><LoaderCircle className="hosted-spin" size={15} />Connecting to the service. First access may take a moment.</p>}
        {!gateReady && !loadingLibrary && <button className="hosted-text-button" onClick={() => window.location.reload()}>Retry connection</button>}
        <p className="hosted-entry-note">Email ownership is not verified. Anyone who knows an allowed email can enter. Each visit has a separate workspace.</p>
      </> : <>
        <div className="hosted-title"><h1>{job?.status === "complete" ? "Comparison report" : job ? "Your comparison" : "New comparison"}</h1>
          <p>{job?.status === "complete" ? "Review the annotated manuscript and matching source passages." : "Upload your manuscript. Get an annotated PDF of matching passages."}</p></div>
        {error && <p role="alert" className="hosted-error">{error}</p>}
        {loadingLibrary && <p role="status" className="hosted-loading"><LoaderCircle className="hosted-spin" size={15} />Loading comparison papers…</p>}
        {!loadingLibrary && papers.length === 0 && <button onClick={loadLibrary}>Retry library connection</button>}
        {!job && <>
          <fieldset disabled={busy} className="hosted-surface hosted-form">
            <section className="hosted-section"><span className="hosted-label">Your manuscript</span><ManuscriptInput file={target} error={fileError} onFile={chooseTarget} onRemove={() => { targetChoice.current++; setTarget(null); setFileError(""); }} /></section>
            <section className="hosted-sources-row"><div><h2>{selected.length} papers selected</h2><p>From the {papers.length}-paper reference library</p></div><button onClick={() => setReviewing(true)} disabled={!papers.length}>Review papers</button></section>
            <section className="hosted-extras">
              <input ref={extraInput} tabIndex={-1} className="hosted-hidden-input" type="file" multiple accept=".pdf,.txt" aria-label="Additional comparison papers" onChange={e => { void addSources(Array.from(e.target.files || [])); e.target.value = ""; }} />
              <button className="hosted-text-button" onClick={() => extraInput.current?.click()}><Plus size={15} />Add your own comparison papers</button>
              {sources.map((file, i) => <div className="hosted-extra-file" key={`${file.name}-${i}`}><FileText size={15} aria-hidden="true" /><span title={file.name}>{file.name}</span><small>{fileSize(file.size)}</small><button className="hosted-icon-button" aria-label={`Remove comparison ${file.name}`} onClick={() => setSources(previous => previous.filter((_, index) => index !== i))}><X size={15} /></button></div>)}
              {sourceError && <p role="alert" className="hosted-field-error">{sourceError}</p>}
            </section>
            <div className="hosted-action-bar"><p id="compare-reason">{busy ? "Uploading your files…" : disabledReason || `Ready to compare against ${selected.length + sources.length} papers.`}</p><button className="hosted-primary" disabled={!!disabledReason || busy} aria-describedby="compare-reason" onClick={compare}>{busy ? "Uploading…" : "Compare papers"}<ArrowRight size={16} aria-hidden="true" /></button></div>
          </fieldset>
          <label className="hosted-consent"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />I’m authorized to upload these files for online comparison.</label>
        </>}
        {job && <section aria-live="polite">
          {job.status === "running" ? <div className="hosted-surface hosted-progress"><LoaderCircle size={24} className="hosted-spin" aria-hidden="true" /><h2>Comparing your manuscript</h2><p>{submitted.title} · {submitted.count} comparison papers</p><p>{stages[job.progress?.stage || ""] || "Starting the comparison"}{job.progress?.source_index && ["load-source", "compare"].includes(job.progress?.stage || "") ? ` · paper ${job.progress.source_index} of ${job.progress.source_count || submitted.count}` : ""}</p><p>{job.progress?.checked_sources !== undefined ? `${job.progress.checked_sources} papers fully checked. ` : ""}Large comparisons can take several minutes; you can cancel at any time.</p><div className="hosted-progress-meta"><Elapsed since={startedAt} /><button onClick={remove}>Cancel comparison</button></div>{error && <button onClick={() => setRetryStatus(value => value + 1)}>Retry status</button>}</div>
          : job.status === "complete" ? <>
            {job.partial && <p className="hosted-critical">{job.score_available === false ? "Comparison incomplete. No score is available." : "Partial comparison: the overlap is a lower bound."} {job.checked} of {job.total} papers were fully checked.</p>}
            {fallbackWarning && <p className="hosted-critical">{fallbackWarning}</p>}
            <div className="hosted-surface">
              <div className="hosted-report-header"><FileText size={24} aria-hidden="true" /><div className="hosted-report-name"><h2>{submitted.title || "Your manuscript"}</h2><p>Annotated PDF · Matching source passages included</p></div></div>
              <div className="hosted-report-metrics"><div><strong>{job.score_available === false ? "Not assessed" : `${job.overlap_percent}%`}</strong><span>Text overlap{job.partial && job.score_available !== false ? " · lower bound" : ""}</span></div><div><strong className="hosted-small-metric">{job.checked} / {job.total}</strong><span>Papers fully checked</span></div></div>
              <div className="hosted-report-actions"><button className="hosted-primary" disabled={pdfBusy} onClick={() => reportFile("pdf", true)}><ExternalLink size={15} />{pdfBusy ? "Preparing…" : "Open PDF"}</button><button disabled={pdfBusy} onClick={() => reportFile("pdf")}><Download size={15} />Download PDF</button></div>
            </div>
            <p className="hosted-report-note">Download before refreshing or leaving. For side comments, open the PDF’s Comments panel in Acrobat Reader.</p>
            <details className="hosted-info"><summary>Report details and evidence</summary><p>Saved engine: {job.algorithm_version || "not recorded"} · Text overlap for review, not a plagiarism verdict. Scores retain the saved comparison basis.</p>{job.warnings?.filter(w => w !== fallbackWarning).map((w, i) => <p key={i}>{w}</p>)}<button onClick={() => reportFile("json")}>Download evidence JSON</button></details>
            <div className="hosted-bottom-actions"><button onClick={newComparison}>New comparison</button><button className="hosted-text-button" onClick={() => { if (window.confirm("Delete this temporary comparison and its report?")) void remove(); }}>Delete report</button></div>
          </> : <div className="hosted-surface hosted-progress"><h2>{job.status === "cancelled" ? "Comparison cancelled" : job.evidence_available ? "Comparison saved; PDF unavailable" : "Couldn’t finish this comparison"}</h2><p>{job.error || "No report was created."}</p>{job.diagnostic_id && <p className="hosted-report-note">Reference {job.diagnostic_id.slice(0, 8)} · {stages[job.progress?.stage || ""] || "Processing"} · {job.error_code}</p>}<div className="hosted-bottom-actions">{job.evidence_available && <button onClick={() => reportFile("json")}>Download evidence JSON</button>}<button onClick={() => { void remove(); }}>Back to setup</button></div></div>}
        </section>}
        <details className="hosted-info"><summary>Privacy, access and source credits</summary><p>Files are processed on Azure, not sent to external AI or discovery providers. This page’s random access token stays in memory. Refreshing or leaving loses access. Server files expire within one hour and may disappear sooner after restart.</p><p>Email ownership is not verified; anyone knowing an allowed email can enter. Separate visitor tokens protect each visitor’s jobs. Do not upload confidential or sensitive manuscripts.</p><p>Manuscripts: 10 MB, 250 pages, 250,000 extracted characters. Up to five added papers, 8 MB each; total upload limit 32 MB. Pages and extractability are checked during comparison. Resource limits can produce partial results.</p><button onClick={() => setReviewing(true)}>Review source credits</button><button onClick={credits}>Download source credits</button></details>
        <p className="hosted-info">Temporary workspace · Download your report before leaving.</p>
      </>}
    </main>
    <footer className="hosted-footer"><span>For research review. Not affiliated with Crossref or Turnitin.</span><a href="https://github.com/OmerHanan1/buna-screening-tool" target="_blank" rel="noreferrer">Source code · AGPL</a></footer>
    {reviewing && <PaperDialog papers={papers} selected={selected} onSelect={setSelected} onClose={() => setReviewing(false)} readOnly={busy || job !== null} />}
  </div>;
}
