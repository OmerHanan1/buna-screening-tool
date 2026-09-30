import { useEffect, useRef, useState } from "react";
import { ArrowRight, FileText, LoaderCircle, Plus, X } from "lucide-react";
import PdfActions from "./PdfActions";
import { Elapsed, fileSize, ManuscriptInput, PaperDialog, validateFile, type HostedPaper } from "./PublicControls";
import "./public.css";

type Job = { id: string; status: string; checked?: number; total?: number; overlap_percent?: number; score_available?: boolean; partial?: boolean; warnings?: string[]; error?: string; algorithm_version?: string; comparison_model?: string; classification_counts?: { exact_words: number; similar_only_words: number; unmatched_words: number; not_fully_checked_words: number }; error_code?: string; diagnostic_id?: string; evidence_available?: boolean; library_saves?: { source_id: string; title: string; state: string; reason: string }[]; progress?: { stage?: string; source_index?: number; source_count?: number; checked_sources?: number; elapsed_seconds?: number } };
const base = (import.meta.env.VITE_PUBLIC_API_URL || "").replace(/\/$/, "");
const gatedMode = import.meta.env.VITE_EMAIL_GATE === "true";
type SourceSave = { id?: string; key: string; state: string; reason?: string; digest?: string };
const savePending = (save: SourceSave) => ["uploading", "receiving", "queued", "validating", "saving", "confirming"].includes(save.state);
const saveReady = (save?: SourceSave) => !!save && ["saved", "already-present"].includes(save.state);
class RequestError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

export default function PublicApp() {
  const [papers, setPapers] = useState<HostedPaper[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [target, setTarget] = useState<File | null>(null);
  const [sources, setSources] = useState<File[]>([]);
  const [keepSources, setKeepSources] = useState<Set<File>>(new Set());
  const [sourceSaves, setSourceSaves] = useState<Map<File, SourceSave>>(new Map());
  const sourceSavesRef = useRef(new Map<File, SourceSave>());
  const libraryRef = useRef<HostedPaper[]>([]);
  const libraryRequest = useRef(0);
  const [sharedAvailable, setSharedAvailable] = useState(false);
  const [libraryWarning, setLibraryWarning] = useState("");
  const [saveRetrying, setSaveRetrying] = useState(false);
  const [job, setJob] = useState<Job | null>(null);
  const [retryStatus, setRetryStatus] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [consent, setConsent] = useState(false);
  const [model, setModel] = useState("validated-lexical");
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
      ...options, signal: options.signal ?? AbortSignal.timeout(90000), credentials: "omit",
      headers: { ...options.headers, ...(token.current ? { Authorization: `Bearer ${token.current}` } : {}) },
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) {
        setEntered(false); token.current = ""; setJob(null); setPapers([]); setSelected([]);
        setModel("validated-lexical");
        const message = "This workspace expired or the service restarted. Enter your email to start a new one.";
        setError(message);
        throw new Error(message);
      }
      if (response.status === 404 && path.startsWith("/jobs/")) {
        setJob(null);
        setError("This comparison is no longer available. Temporary reports expire or may be lost after a service restart.");
      }
      throw new RequestError(typeof data.detail === "string" ? data.detail : `Request failed (${response.status}).`, response.status);
    }
    return response;
  }
  async function loadLibrary(preserveSelection = false) {
    const sequence = ++libraryRequest.current;
    setLoadingLibrary(true); setError("");
    try {
      const response = gatedMode ? await request("/library") : await fetch(`${base}/api/public/library`, { credentials: "omit", signal: AbortSignal.timeout(90000) });
      if (!response.ok) throw new Error("The comparison library could not be loaded. Please retry.");
      const data = await response.json();
      if (sequence !== libraryRequest.current) return libraryRef.current;
      const previous = new Set(libraryRef.current.map(paper => paper.sha256));
      libraryRef.current = data.papers;
      setPapers(data.papers);
      setSelected(current => data.papers.filter((paper: HostedPaper) => !preserveSelection || !previous.has(paper.sha256) || current.includes(paper.sha256)).map((paper: HostedPaper) => paper.sha256));
      setSharedAvailable(data.shared_saving_available === true && data.immediate_shared_saving === true);
      setLibraryWarning(data.shared_library_warning || "");
      return data.papers as HostedPaper[];
    } catch (e) { if (sequence === libraryRequest.current) setError(e instanceof Error ? e.message : "The service could not be reached. Retry shortly."); }
    finally { if (sequence === libraryRequest.current) setLoadingLibrary(false); }
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
    if (!job || (job.status !== "running" && !job.library_saves?.some(save => ["pending", "saving"].includes(save.state))) || (gatedMode && !entered)) return;
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
  }, [job?.id, job?.status, job?.library_saves?.some(save => ["pending", "saving"].includes(save.state)), retryStatus, entered]);

  function updateSourceSave(file: File, value: SourceSave) {
    const next = new Map(sourceSavesRef.current); next.set(file, value);
    sourceSavesRef.current = next; setSourceSaves(next);
  }
  async function confirmSourceSave(file: File, value: SourceSave) {
    updateSourceSave(file, { ...value, state: "confirming" });
    const library = await loadLibrary(true);
    if (!library?.some(paper => paper.sha256 === value.digest)) {
      updateSourceSave(file, { ...value, state: "failed", reason: "Storage reported completion, but library visibility could not be confirmed. Retry to check again." });
      return;
    }
    updateSourceSave(file, value);
  }
  async function beginSourceSave(file: File) {
    let prior = sourceSavesRef.current.get(file);
    if (prior && (savePending(prior) || saveReady(prior))) return;
    if (prior?.state === "cancelled") prior = undefined;
    const value: SourceSave = { ...prior, key: prior?.key || crypto.randomUUID(), state: "uploading", reason: "" };
    updateSourceSave(file, value);
    try {
      if (target) {
        const hashes = await Promise.all([file, target].map(async item => {
          const digest = await crypto.subtle.digest("SHA-256", await item.arrayBuffer());
          return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
        }));
        if (hashes[0] === hashes[1]) throw new Error("This is identical to your current manuscript. It was not shared.");
      }
      let result;
      if (prior?.id) {
        try {
          const current = await (await request(`/source-saves/${prior.id}`)).json();
          result = saveReady(current) || savePending(current) ? current : await (await request(`/source-saves/${prior.id}/retry`, { method: "POST" })).json();
        } catch (e) {
          if (!(e instanceof RequestError) || e.status !== 404) throw e;
          value.id = undefined; value.key = crypto.randomUUID();
        }
      }
      if (!result) {
        const form = new FormData(); form.append("source", file); form.append("share_authorized", "true");
        result = await (await request("/source-saves", { method: "POST", body: form, headers: { "Idempotency-Key": value.key } })).json();
      }
      const updated = { ...value, ...result };
      if (saveReady(updated)) await confirmSourceSave(file, updated);
      else updateSourceSave(file, updated);
    } catch (e) {
      updateSourceSave(file, { ...value, ...(e instanceof RequestError && e.status === 404 ? { id: undefined, key: crypto.randomUUID() } : {}), state: "failed", reason: (e as Error).message });
    }
  }
  async function cancelSourceSave(file: File) {
    const value = sourceSavesRef.current.get(file);
    if (!value?.id) return;
    try {
      const result = await (await request(`/source-saves/${value.id}`, { method: "DELETE" })).json();
      updateSourceSave(file, { ...value, ...result });
      setKeepSources(previous => { const next = new Set(previous); next.delete(file); return next; });
    } catch (e) { setSourceError((e as Error).message); }
  }
  const pendingSourceSaves = [...sourceSaves.values()].some(savePending);
  useEffect(() => {
    if (!pendingSourceSaves || !entered) return;
    let stopped = false, polling = false;
    const timer = window.setInterval(async () => {
      if (polling) return;
      polling = true;
      try { for (const [file, value] of sourceSavesRef.current) {
        if (!value.id || !["queued", "validating", "saving", "receiving"].includes(value.state)) continue;
        try {
          const result = await (await request(`/source-saves/${value.id}`)).json();
          if (stopped) return;
          const updated = { ...value, ...result };
          if (saveReady(updated)) await confirmSourceSave(file, updated);
          else updateSourceSave(file, updated);
        } catch (e) {
          if (!stopped) updateSourceSave(file, { ...value, state: "failed", reason: (e as Error).message });
        }
      } } finally { polling = false; }
    }, 2000);
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => { stopped = true; window.clearInterval(timer); window.removeEventListener("beforeunload", warn); };
  }, [pendingSourceSaves, entered]);

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
    if (pendingSourceSaves) { setError("Selected papers are still being saved. Wait for Saved to shared library before leaving."); return; }
    if ([...sourceSaves.values()].some(save => save.state === "failed") && !window.confirm("Some selected papers were not confirmed saved. Leaving loses access to retry with these files. Leave anyway?")) return;
    if (job?.status === "running") { setError("Cancel the running comparison before leaving this workspace."); return; }
    if (job?.library_saves?.some(save => ["pending", "saving"].includes(save.state))) { setError("Shared saving is still finishing. Wait for its status before leaving."); return; }
    if (job?.status === "complete" && !window.confirm("Download your report first. Leaving removes this page’s access to it. Leave workspace?")) return;
    token.current = ""; setEntered(false); urls.current.forEach(URL.revokeObjectURL); urls.current = [];
    submission.current = null;
    setJob(null); setPapers([]); setSelected([]); setTarget(null); setSources([]); setConsent(false); setError("");
    setKeepSources(new Set()); setSharedAvailable(false); setLibraryWarning("");
    sourceSavesRef.current = new Map(); setSourceSaves(new Map()); libraryRef.current = [];
    setModel("validated-lexical");
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
    if (!target || !consent || busy || pendingSourceSaves) return;
    const readyDigests = sources.flatMap(file => {
      const saved = sourceSavesRef.current.get(file);
      return saveReady(saved) && saved?.digest ? [saved.digest] : [];
    });
    const comparisonSelected = [...new Set([...selected, ...readyDigests])];
    const comparisonUploads = sources.filter(file => !saveReady(sourceSavesRef.current.get(file)));
    setBusy(true); setError("");
    setSubmitted({ title: target.name, count: comparisonSelected.length + comparisonUploads.length });
    try {
      if (!gatedMode && !token.current) token.current = (await (await request("/session", { method: "POST" })).json()).token;
      const form = new FormData();
      form.append("target", target); form.append("selected", JSON.stringify(comparisonSelected));
      form.append("comparison_model", model);
      comparisonUploads.forEach(file => form.append("sources", file));
      const fingerprint = JSON.stringify({ target: [target.name, target.size, target.lastModified], selected: comparisonSelected, model,
        sources: comparisonUploads.map(file => [file.name, file.size, file.lastModified]) });
      if (submission.current?.fingerprint !== fingerprint) submission.current = { key: crypto.randomUUID(), fingerprint };
      const created = await (await request("/jobs", { method: "POST", body: form, headers: { "Idempotency-Key": submission.current.key } })).json();
      const current = created.status === "running" ? created : await (await request(`/jobs/${created.id}`)).json();
      if (created.source_count !== undefined) setSubmitted(previous => ({ ...previous, count: created.source_count }));
      setStartedAt(Date.now()); setJob(current);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function reportFile(format: "json") {
    if (!job || pdfBusy) return;
    setPdfBusy(true); setError("");
    try {
      const blob = await (await request(`/jobs/${job.id}/report.${format}`)).blob();
      const url = URL.createObjectURL(blob); urls.current.push(url);
      const link = document.createElement("a"); link.href = url; link.download = `paper-overlap-report.${format}`;
      document.body.appendChild(link); link.click(); link.remove();
    } catch (e) { setError((e as Error).message); }
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
      if (value.status === "deleted") { submission.current = null; setModel("validated-lexical"); }
    } catch (e) { setError((e as Error).message); }
  }
  async function retrySharedSave() {
    if (!job || saveRetrying) return;
    setSaveRetrying(true); setError("");
    try {
      await request(`/jobs/${job.id}/library-save-retry`, { method: "POST" });
      setJob({ ...job, library_saves: job.library_saves?.map(save => save.state === "failed" ? { ...save, state: "saving" } : save) });
      setRetryStatus(value => value + 1);
    } catch (e) { setError((e as Error).message); }
    finally { setSaveRetrying(false); }
  }
  function newComparison() {
    if (pendingSourceSaves) { setError("Wait for selected papers to finish saving."); return; }
    if ([...sourceSaves.values()].some(save => save.state === "failed") && !window.confirm("Some selected papers were not confirmed saved. Start over without saving them?")) return;
    if (job?.library_saves?.some(save => ["pending", "saving"].includes(save.state))) { setError("Wait for shared saving to finish before starting a new comparison."); return; }
    if (!window.confirm("Keep your downloaded report before starting a new comparison. Continue?")) return;
    submission.current = null;
    setModel("validated-lexical");
    setJob(null); setTarget(null); setSources([]); setConsent(false); setError(""); setFileError(""); setSourceError("");
    setKeepSources(new Set());
    sourceSavesRef.current = new Map(); setSourceSaves(new Map());
    void loadLibrary(true);
  }
  const hasAccess = !gatedMode || entered;
  const totalUpload = (target?.size || 0) + sources.reduce((sum, file) => sum + file.size, 0);
  const comparisonCount = new Set([...selected, ...sources.flatMap(file => {
    const saved = sourceSaves.get(file); return saveReady(saved) && saved?.digest ? [saved.digest] : [];
  })]).size + sources.filter(file => !saveReady(sourceSaves.get(file))).length;
  const disabledReason = pendingSourceSaves ? "Saving selected papers first. No comparison is needed to save them." : loadingLibrary ? "Loading comparison papers…" : !target ? "Add your manuscript to begin." : !selected.length && !sources.length ? "Select at least one comparison paper."
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
        {!loadingLibrary && papers.length === 0 && <button onClick={() => loadLibrary()}>Retry library connection</button>}
        {libraryWarning && <p role="alert" className="hosted-critical">{libraryWarning}</p>}
        {!job && <>
          <fieldset disabled={busy} className="hosted-surface hosted-form">
            <section className="hosted-section"><span className="hosted-label">Your manuscript</span><ManuscriptInput file={target} error={fileError} onFile={chooseTarget} onRemove={() => { targetChoice.current++; setTarget(null); setFileError(""); }} /></section>
            <section className="hosted-sources-row"><div><h2>{selected.length} papers selected</h2><p>From the {papers.length}-paper comparison library</p></div><button onClick={() => setReviewing(true)} disabled={!papers.length}>Review papers</button></section>
            <section className="hosted-extras">
              <input ref={extraInput} tabIndex={-1} className="hosted-hidden-input" type="file" multiple accept=".pdf,.txt" aria-label="Additional comparison papers" onChange={e => { void addSources(Array.from(e.target.files || [])); e.target.value = ""; }} />
              <button className="hosted-text-button" onClick={() => extraInput.current?.click()}><Plus size={15} />Add your own comparison papers</button>
              {sources.map((file, i) => <div key={`${file.name}-${i}`}>
                <div className="hosted-extra-file"><FileText size={15} aria-hidden="true" /><span title={file.name}>{file.name}</span><small>{fileSize(file.size)}</small><button disabled={!!sourceSaves.get(file) && savePending(sourceSaves.get(file)!)} className="hosted-icon-button" aria-label={`Remove comparison ${file.name}`} onClick={() => { const digest = sourceSaves.get(file)?.digest; if (digest) setSelected(previous => previous.filter(id => id !== digest)); setSources(previous => previous.filter((_, index) => index !== i)); setKeepSources(previous => { const next = new Set(previous); next.delete(file); return next; }); }}><X size={15} /></button></div>
                {sharedAvailable && /\.pdf$/i.test(file.name) && <label className="hosted-shared-option"><input type="checkbox" checked={keepSources.has(file)} disabled={!!sourceSaves.get(file) && savePending(sourceSaves.get(file)!)} onChange={e => { const checked = e.target.checked; setKeepSources(previous => { const next = new Set(previous); if (checked) next.add(file); else next.delete(file); return next; }); if (checked) void beginSourceSave(file); }} />Keep in library for future comparisons<span className="hosted-sr-only">: {file.name}</span></label>}
                {sourceSaves.has(file) && <div className="hosted-shared-notice" role="status" aria-label={`Save status: ${file.name}`}>
                  {saveReady(sourceSaves.get(file)) ? <><strong>{sourceSaves.get(file)?.state === "already-present" ? "Already in shared library." : "Saved to shared library."}</strong> Listed as “{papers.find(paper => paper.sha256 === sourceSaves.get(file)?.digest)?.title}” in Review papers. Removing or unchecking here does not delete the saved paper.</>
                    : sourceSaves.get(file)?.state === "failed" ? <><strong>Not confirmed saved.</strong> {sourceSaves.get(file)?.reason} <button onClick={() => void beginSourceSave(file)}>Retry save</button></>
                    : sourceSaves.get(file)?.state === "cancelled" ? "Save cancelled. The file was not added by this request."
                    : sourceSaves.get(file)?.state === "queued" ? "Saving queued — waiting for the current parser/comparison to finish."
                    : "Saving… validating the PDF and confirming permanent library storage."}
                  {sourceSaves.get(file)?.id && <small> Save reference: {sourceSaves.get(file)?.id}</small>}
                  {["queued", "validating"].includes(sourceSaves.get(file)?.state || "") && <button onClick={() => void cancelSourceSave(file)}>Cancel save</button>}
                </div>}
              </div>)}
              {sharedAvailable && sources.some(file => /\.pdf$/i.test(file.name)) && <p className="hosted-shared-notice">Checking the box immediately uploads and saves that comparison PDF; no manuscript or comparison is required. Only check it if you’re authorized to store and share it for hosted comparisons and matching excerpts. Saved papers are available to everyone with app access. Wait for “Saved to shared library” before leaving; an unfinished save may be lost after a service restart.</p>}
              {sourceError && <p role="alert" className="hosted-field-error">{sourceError}</p>}
              <details className="hosted-model-details"><summary>{model === "classified-v1.1" ? "Advanced · experimental model selected" : "Advanced"}</summary>
                <label htmlFor="comparison-model">Comparison model</label>
                <select id="comparison-model" value={model} onChange={e => setModel(e.target.value)}>
                  <option value="validated-lexical">Standard wording comparison</option>
                  <option value="classified-v1.1">Exact + similar wording (experimental)</option>
                </select>
                {model === "classified-v1.1" && <p>Experimental: separates exact wording from bounded word edits/reordering. Scores can differ; no accuracy or Crossref-equivalence claim.</p>}
              </details>
            </section>
            <div className="hosted-action-bar"><p id="compare-reason">{busy ? "Uploading your files…" : disabledReason || `Ready to compare against ${comparisonCount} papers.`}</p><button className="hosted-primary" disabled={!!disabledReason || busy} aria-describedby="compare-reason" onClick={compare}>{busy ? "Uploading…" : "Compare papers"}<ArrowRight size={16} aria-hidden="true" /></button></div>
          </fieldset>
          <label className="hosted-consent"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />I’m authorized to upload these files for online comparison.</label>
        </>}
        {job && <section aria-live="polite">
          {job.status === "running" ? <div className="hosted-surface hosted-progress"><LoaderCircle size={24} className="hosted-spin" aria-hidden="true" /><h2>Comparing your manuscript</h2><p>{submitted.title} · {submitted.count} comparison papers</p><p>{stages[job.progress?.stage || ""] || "Starting the comparison"}{job.progress?.source_index && ["load-source", "compare"].includes(job.progress?.stage || "") ? ` · paper ${job.progress.source_index} of ${job.progress.source_count || submitted.count}` : ""}</p><p>{job.progress?.checked_sources !== undefined ? `${job.progress.checked_sources} papers fully checked. ` : ""}Large comparisons can take several minutes; you can cancel at any time.</p><div className="hosted-progress-meta"><Elapsed since={startedAt} /><button onClick={remove}>Cancel comparison</button></div>{error && <button onClick={() => setRetryStatus(value => value + 1)}>Retry status</button>}</div>
          : job.status === "complete" ? <>
            {job.comparison_model === "classified-v1.1" && <p className="hosted-critical">Experimental exact + similar wording report. Scores may differ from the standard model.</p>}
            {job.partial && <p className="hosted-critical">{job.score_available === false ? "Comparison incomplete. No score is available." : "Partial comparison: the overlap is a lower bound."} {job.checked} of {job.total} papers were fully checked.</p>}
            {fallbackWarning && <p className="hosted-critical">{fallbackWarning}</p>}
            <div className="hosted-surface">
              <div className="hosted-report-header"><FileText size={24} aria-hidden="true" /><div className="hosted-report-name"><h2>{submitted.title || "Your manuscript"}</h2><p>Annotated PDF · Matching source passages included</p></div></div>
              <div className="hosted-report-metrics"><div><strong>{job.score_available === false ? "Not assessed" : `${job.overlap_percent}%`}</strong><span>Text overlap{job.partial && job.score_available !== false ? " · lower bound" : ""}</span></div><div><strong className="hosted-small-metric">{job.checked} / {job.total}</strong><span>Papers fully checked</span></div></div>
              <PdfActions jobId={job.id} load={signal => request(`/jobs/${job.id}/report.pdf`, { signal })} />
            </div>
            <p className="hosted-report-note">Download before refreshing or leaving. For side comments, open the PDF’s Comments panel in Acrobat Reader.</p>
            <details className="hosted-info"><summary>Report details and evidence</summary><p>Saved engine: {job.algorithm_version || "not recorded"} · Text overlap for review, not a plagiarism verdict. Scores retain the saved comparison basis.</p>
              {job.classification_counts && <p>{job.classification_counts.exact_words} exact words · {job.classification_counts.similar_only_words} similar-only words. {job.classification_counts.not_fully_checked_words ? `${job.classification_counts.not_fully_checked_words} words are not fully checked.` : `${job.classification_counts.unmatched_words} eligible words had no match found in the checked sources; this is not a finding of originality.`}</p>}
              {job.warnings?.filter(w => w !== fallbackWarning).map((w, i) => <p key={i}>{w}</p>)}<button onClick={() => reportFile("json")}>Download evidence JSON</button></details>
            <div className="hosted-bottom-actions"><button onClick={newComparison}>New comparison</button><button className="hosted-text-button" onClick={() => { if (window.confirm("Delete this temporary comparison and its report?")) void remove(); }}>Delete report</button></div>
          </> : <div className="hosted-surface hosted-progress"><h2>{job.status === "cancelled" ? "Comparison cancelled" : job.evidence_available ? "Comparison saved; PDF unavailable" : "Couldn’t finish this comparison"}</h2><p>{job.error || "No report was created."}</p>{job.diagnostic_id && <p className="hosted-report-note">Reference {job.diagnostic_id.slice(0, 8)} · {stages[job.progress?.stage || ""] || "Processing"} · {job.error_code}</p>}<div className="hosted-bottom-actions">{job.evidence_available && <button onClick={() => reportFile("json")}>Download evidence JSON</button>}<button onClick={() => { void remove(); }}>Back to setup</button></div></div>}
        </section>}
        {job?.library_saves?.length ? <section className="hosted-info" aria-label="Shared library save status">
          <strong>Shared library</strong>
          {job.library_saves.map(save => <p key={save.source_id}>{save.title}: {save.state === "saved" ? "Saved for future comparisons." : save.state === "already-present" ? "Already in the library." : ["pending", "saving"].includes(save.state) ? "Saving…" : save.reason || "Not saved."}</p>)}
          {job.library_saves.some(save => save.state === "failed") && <button disabled={saveRetrying} onClick={retrySharedSave}>Retry library save</button>}
        </section> : null}
        {job && sourceSaves.size > 0 && <section className="hosted-info" aria-label="Independent shared saves">
          <strong>Shared library</strong>
          {[...sourceSaves].map(([file, save]) => <p key={save.key}>{file.name}: {saveReady(save) ? "Saved in shared library." : savePending(save) ? "Saving independently of this comparison…" : save.reason || "Not saved."}
            {save.state === "failed" && <button onClick={() => void beginSourceSave(file)}>Retry save</button>}
            {save.id && <small> Save reference: {save.id}</small>}</p>)}
        </section>}
        <details className="hosted-info"><summary>Privacy, access and source credits</summary><p>Files are processed on Azure, not sent to external AI or discovery providers. This page’s random access token stays in memory. Refreshing or leaving loses access. Manuscripts, reports and unsaved comparison files expire within one hour and may disappear sooner after restart. Comparison PDFs explicitly kept in the shared library persist for future visitors; the manuscript is never saved by that option.</p><p>Email ownership is not verified; anyone knowing an allowed email can enter and use shared papers for comparisons. Separate visitor tokens protect each visitor’s jobs. Do not upload confidential or sensitive manuscripts.</p><p>Manuscripts: 10 MB, 250 pages, 250,000 extracted characters. Up to five added papers, 8 MB each; total upload limit 32 MB. Pages and extractability are checked during comparison. Resource limits can produce partial results.</p><button onClick={() => setReviewing(true)}>Review source credits</button><button onClick={credits}>Download source credits</button></details>
        <p className="hosted-info">Temporary workspace · Download your report before leaving.</p>
      </>}
    </main>
    <footer className="hosted-footer"><span>For research review. Not affiliated with Crossref or Turnitin.</span><a href="https://github.com/OmerHanan1/buna-screening-tool" target="_blank" rel="noreferrer">Source code · AGPL</a></footer>
    {reviewing && <PaperDialog papers={papers} selected={selected} onSelect={setSelected} onClose={() => setReviewing(false)} readOnly={busy || job !== null} />}
  </div>;
}
