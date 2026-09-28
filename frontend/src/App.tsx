import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ArrowDownToLine, ArrowRight, BookOpen, Check, ChevronDown, CircleAlert, FileText, Layers3, LoaderCircle, LockKeyhole, Plus, Search, ShieldCheck, Upload, X } from "lucide-react";
import { api, isActive, isTestFixture, json, reportAvailable, safeUrl } from "./api";
import { comparisonState, highlightedFragments, reuseCategory, sourceReason } from "./evidence";
import type { Consent, Job, Paper, Provider, Reference, Report, Segment } from "./types";

type Settings = { queries: string[]; references: Reference[]; target: number; provider: string; mode: "online" | "offline" };
const humanStages: Record<string, string> = {
  extraction: "Reading your paper", discovery: "Finding papers", acquisition: "Downloading available papers",
  comparison: "Comparing sentences", report: "Preparing your report", done: "Your report is ready",
};

function startingJob(): string | null {
  const fromUrl = new URLSearchParams(window.location.search).get("job");
  if (fromUrl) return fromUrl;
  try { return localStorage.getItem("buna-current-paper"); } catch { return null; }
}

function rememberJob(id: string | null) {
  const url = new URL(window.location.href);
  if (id) url.searchParams.set("job", id); else url.searchParams.delete("job");
  window.history.replaceState(null, "", url);
  try {
    if (id) localStorage.setItem("buna-current-paper", id); else localStorage.removeItem("buna-current-paper");
  } catch { /* Storage may be disabled; the URL still restores this paper. */ }
}

function includeTestPapers(): boolean {
  if (new URLSearchParams(window.location.search).get("include_test") === "true") return true;
  try { return localStorage.getItem("buna-show-test-papers") === "1"; } catch { return false; }
}

function Note({ children, alert = false }: { children: ReactNode; alert?: boolean }) {
  return <div className={`simple-note ${alert ? "attention" : ""}`} role={alert ? "alert" : undefined}>
    {alert ? <CircleAlert size={18} /> : <LockKeyhole size={17} />}<div>{children}</div>
  </div>;
}

export default function App() {
  const [selected, setSelected] = useState<string | null>(startingJob);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [showFixtures, setShowFixtures] = useState(includeTestPapers);
  const [listedJobIds, setListedJobIds] = useState<Set<string> | null>(null);
  const selectedRef = useRef(selected);
  const choseNewPaper = useRef(false);
  const ownedSelections = useRef(new Set<string>());
  const jobsRef = useRef(jobs);
  selectedRef.current = selected;
  jobsRef.current = jobs;
  const update = useCallback((next: Job) => {
    if (selectedRef.current === next.id) setJob(next);
    setJobs((current) => current.some((item) => item.id === next.id)
      ? current.map((item) => item.id === next.id ? next : item) : [next, ...current]);
  }, []);

  useEffect(() => {
    let stale = false;
    api<Job[]>(showFixtures ? "/jobs?include_test=true" : "/jobs").then((values) => {
      if (stale) return;
      setJobs(values);
      setListedJobIds(new Set(values.map((item) => item.id)));
      const restorable = values.find((item) => !isTestFixture(item) && isActive(item.status))
        || values.find((item) => !isTestFixture(item) && reportAvailable(item));
      if (restorable && !selectedRef.current && !choseNewPaper.current) setSelected(restorable.id);
    }).catch((err: Error) => { if (!stale) setError(err.message); });
    return () => { stale = true; };
  }, [showFixtures]);

  useEffect(() => {
    api<{ providers: Provider[] }>("/providers").then((result) => setProviders(result.providers))
      .catch((err: Error) => setError(err.message));
  }, []);

  useEffect(() => {
    rememberJob(selected);
    if (!selected) { setJob(null); return; }
    if (!listedJobIds) return;
    if (!listedJobIds.has(selected) && !ownedSelections.current.has(selected)) {
      const realJobs = jobsRef.current.filter((item) => !isTestFixture(item));
      const fallback = realJobs.find((item) => isActive(item.status)) || realJobs.find(reportAvailable) || realJobs[0];
      setSelected(fallback?.id || null);
      return;
    }
    let stale = false;
    setLoading(true); setJob(null); setError("");
    api<Job>(`/jobs/${encodeURIComponent(selected)}`).then((value) => {
      if (stale) return;
      if (isTestFixture(value) && !listedJobIds.has(value.id) && !ownedSelections.current.has(value.id)) {
        const realJobs = jobsRef.current.filter((item) => !isTestFixture(item));
        const fallback = realJobs.find((item) => isActive(item.status)) || realJobs.find(reportAvailable) || realJobs[0];
        setSelected(fallback?.id || null);
        return;
      }
      update(value);
    })
      .catch((err: Error) => { if (!stale) setError(err.message); })
      .finally(() => { if (!stale) setLoading(false); });
    return () => { stale = true; };
  }, [selected, listedJobIds, update]);

  useEffect(() => {
    if (!job || !isActive(job.status)) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await api<Job>(`/jobs/${job.id}`);
        if (!stopped) update(value);
      } catch (err) { if (!stopped) setError(`We couldn’t refresh progress. Your check may still be running. ${(err as Error).message}`); }
      if (!stopped) timer = setTimeout(poll, 1200);
    };
    timer = setTimeout(poll, 500);
    return () => { stopped = true; clearTimeout(timer); };
  }, [job?.id, job?.status, update]);

  const act = async (action: () => Promise<void>) => {
    setBusy(true); setError("");
    try { await action(); } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  };
  const upload = (file: File) => void act(async () => {
    if (!/\.(pdf|txt)$/i.test(file.name)) throw new Error("Please choose a PDF or TXT file.");
    const body = new FormData(); body.append("file", file);
    const created = await api<Job>("/jobs", { method: "POST", body });
    const prepared = await api<Job>(`/jobs/${created.id}`, json("PATCH", {
      queries: created.queries || [], references: created.references || [], target: 150,
    }));
    setJobs((current) => [prepared, ...current.filter((item) => item.id !== prepared.id)]);
    ownedSelections.current.add(prepared.id);
    setSelected(prepared.id);
  });
  const visibleJobs = jobs.filter((item) => showFixtures || !isTestFixture(item));
  return <div className="simple-app">
    <a className="skip-link" href="#paper-workspace">Skip to your paper</a>
    <header className="simple-header">
      <a className="brand" href="./" aria-label="Paper Overlap Detector home"><span className="brand-mark"><Layers3 size={22} /></span><span>Paper Overlap Detector</span></a>
      <div className="simple-header-actions">
        {!!visibleJobs.length && <details className="saved-papers"><summary>Your papers <ChevronDown size={14} /></summary><nav aria-label="Your saved papers">
          {visibleJobs.map((item) => <button key={item.id} disabled={busy} onClick={() => { setSelected(item.id); window.scrollTo({ top: 0 }); }}>
            <FileText size={16} /><span>{item.title || "Untitled paper"}<small>{isActive(item.status) ? "Checking…" : reportAvailable(item) ? "View results" : "Ready to check"}</small></span>
          </button>)}
        </nav></details>}
        <button className="button secondary" disabled={busy} onClick={() => { choseNewPaper.current = true; setSelected(null); setError(""); window.scrollTo({ top: 0 }); }}><Plus size={15} />New paper</button>
      </div>
    </header>
    <main id="paper-workspace" className="simple-main">
      {error && <Note alert><strong>Something needs attention.</strong><p>{error}</p></Note>}
      {!selected ? <Welcome onUpload={upload} busy={busy} />
        : loading || !job ? <div className="simple-loading" role="status"><LoaderCircle className="spin" size={26} /><p>{loading ? "Opening your paper…" : "We couldn’t open this paper. Choose New paper to start again."}</p></div>
          : <PaperScreen key={job.id} job={job} providers={providers} update={update} act={act} busy={busy} />}
      <footer className="simple-footer"><span><ShieldCheck size={15} />Your paper stays on this computer.</span><span>Paper Overlap Detector finds matching wording. You decide what it means.</span></footer>
      <details className="workspace-options"><summary>Workspace options</summary><label><input type="checkbox" checked={showFixtures} onChange={(event) => {
        const value = event.target.checked; setShowFixtures(value);
        try { if (value) localStorage.setItem("buna-show-test-papers", "1"); else localStorage.removeItem("buna-show-test-papers"); } catch { /* History visibility remains usable without local storage. */ }
      }} />Show test papers in history</label><p className="small muted">Only papers explicitly tagged as tests are hidden, never papers identified by their title.</p></details>
    </main>
  </div>;
}

function Welcome({ onUpload, busy }: { onUpload: (file: File) => void; busy: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const receive = (file?: File) => { if (file && !busy) onUpload(file); };
  return <section className="simple-welcome">
    <div className="simple-eyebrow">A second look at your writing</div>
    <h1>Check your paper.<br /><span>See the matching sentences.</span></h1>
    <p className="simple-intro">Upload your paper. We’ll find related research and show you wording worth reviewing—side by side.</p>
    <div className={`simple-upload card ${dragging ? "dragging" : ""}`}
      onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)}
      onDrop={(event) => { event.preventDefault(); setDragging(false); receive(event.dataTransfer.files[0]); }}>
      <div className="simple-upload-icon">{busy ? <LoaderCircle className="spin" size={30} /> : <Upload size={30} />}</div>
      <h2>1. Upload your paper</h2>
      <p>Choose a PDF or text file, or drop it here.</p>
      <button className="button primary simple-primary" disabled={busy} onClick={() => input.current?.click()}>{busy ? "Reading your paper…" : "Upload your paper"}<ArrowRight size={17} /></button>
      <input ref={input} hidden type="file" accept=".pdf,.txt" aria-label="Upload your paper file" disabled={busy}
        onChange={(event) => { receive(event.target.files?.[0]); event.target.value = ""; }} />
      <span className="small muted">Your paper is read here, not uploaded to search providers.</span>
    </div>
    <div className="simple-steps"><span><span>1</span>Upload your paper</span><span><span>2</span>Allow a search</span><span><span>3</span>Review matching sentences</span></div>
  </section>;
}

function PaperScreen({ job, providers, update, act, busy }: {
  job: Job; providers: Provider[]; update: (job: Job) => void; act: (action: () => Promise<void>) => Promise<void>; busy: boolean;
}) {
  const [settings, setSettings] = useState<Settings>({ queries: job.queries || [], references: job.references || [], target: job.target || 150, provider: "crossref", mode: "online" });
  const [dirty, setDirty] = useState(false);
  const [consent, setConsent] = useState<Consent | null>(null);
  const [acknowledged, setAcknowledged] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [revision, setRevision] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const advanced = useRef<HTMLDetailsElement>(null);
  const active = isActive(job.status);
  const showReport = reportAvailable(job) && !retrying && !active;
  const locked = active || busy;
  const documentText = job.document?.text || job.document?.segments?.map((segment) => segment.text).join("\n\n") || "";
  const readable = !!documentText.trim();
  const readySources = (job.papers || []).filter((paper) => ["parsed", "compared"].includes(paper.status)).length;
  const resetConsent = () => { setConsent(null); setAcknowledged(false); };
  const edit = (patch: Partial<Settings>) => { setSettings((current) => ({ ...current, ...patch })); setDirty(true); resetConsent(); };

  useEffect(() => {
    if (active || showReport || dirty || settings.mode === "offline" || !readable) return;
    let cancelled = false;
    resetConsent(); setPreparing(true); setPreviewError("");
    api<Consent>(`/jobs/${job.id}/consent-preview`, json("POST", { provider: settings.provider }))
      .then((value) => { if (!cancelled) setConsent(value); })
      .catch((err: Error) => { if (!cancelled) setPreviewError(err.message); })
      .finally(() => { if (!cancelled) setPreparing(false); });
    return () => { cancelled = true; };
  }, [job.id, active, showReport, dirty, settings.mode, settings.provider, revision, readable]);

  useEffect(() => { window.scrollTo({ top: 0 }); }, [active, showReport]);
  const save = async () => {
    if (!Number.isInteger(settings.target) || settings.target < 100 || settings.target > 200) throw new Error("The related-paper target must be a whole number from 100 to 200.");
    const next = await api<Job>(`/jobs/${job.id}`, json("PATCH", {
      queries: settings.queries.filter((query) => query.trim()), references: settings.references, target: settings.target,
    }));
    update(next); setDirty(false); resetConsent(); setRevision((value) => value + 1);
  };
  const run = () => void act(async () => {
    if (dirty) throw new Error("Save your advanced changes before checking.");
    if (settings.mode === "online" && (!consent || !acknowledged)) throw new Error("Please allow the search before checking.");
    if (settings.mode === "offline" && !readySources) throw new Error("Add source articles in Advanced settings before checking offline.");
    const next = await api<Job>(`/jobs/${job.id}/run`, json("POST", {
      mode: settings.mode, provider: settings.provider, ...(settings.mode === "online" ? { consent_token: consent!.token } : {}),
    }));
    resetConsent(); setRetrying(false); if (advanced.current) advanced.current.open = false; update(next);
  });
  const recover = () => {
    if (locked) return;
    resetConsent(); setRetrying(true); setSettings((current) => ({ ...current, mode: "online", provider: "crossref" }));
    setRevision((value) => value + 1); if (advanced.current) advanced.current.open = false; window.scrollTo({ top: 0 });
  };
  return <div className="paper-screen">
    <div className="simple-paper-name"><FileText size={17} /><span>{job.title || "Your paper"}</span></div>
    {active ? <Progress job={job} busy={busy} onCancel={() => void act(async () => update(await api<Job>(`/jobs/${job.id}/cancel`, { method: "POST" })))} />
      : showReport ? <SimpleResults job={job} onRecover={recover} />
        : <section className="simple-permission">
          {["failed", "cancelled"].includes(job.status) && <Note alert>{job.status === "cancelled" ? "This check was stopped." : "The previous check stopped before results were ready."} You can review the permission below and try again. Nothing restarts automatically.</Note>}
          <div className="simple-eyebrow">Your paper is ready</div>
          <h1>{retrying ? "Let’s find papers to compare." : "2. Ready to check?"}</h1>
          <p className="simple-intro">We’ll look for about {settings.target} related papers, plus the works you reference, and compare the full texts we can access.</p>
          {!readable ? <Note alert><strong>We couldn’t read this file.</strong><p>A scanned PDF may need text recognition first. Please upload a readable PDF or text file.</p></Note>
            : settings.mode === "online" ? <div className="simple-privacy card">
              <div className="privacy-icon"><ShieldCheck size={24} /></div>
              <h2>Your paper stays private.</h2>
              <p>{settings.provider === "crossref"
                ? "To find papers, we share search terms and references with Crossref, then public paper identifiers with OpenAlex. Your paper and uploaded files stay on this computer."
                : "To find papers, we share search terms and references with OpenAlex. Your paper and uploaded files stay on this computer."}</p>
              {preparing && <p className="preview-preparing" role="status"><LoaderCircle className="spin" size={15} />Preparing the details for your permission…</p>}
              {previewError && <Note alert><p>{previewError}</p><button className="button secondary" onClick={() => setRevision((value) => value + 1)}>Try preparing again</button></Note>}
              {consent && <details className="exact-preview"><summary>See exactly what will be shared <ChevronDown size={15} /></summary>
                <p>{consent.description}</p><h3>Where it goes</h3>
                <ul className="simple-destinations">{consent.destinations.map((destination, index) => <li key={index}><code>{destination}</code></li>)}</ul>
                <h3>Exact search details</h3><pre className="simple-consent-payload">{JSON.stringify(consent.payload, null, 2)}</pre>
              </details>}
              <label className="simple-consent"><input type="checkbox" disabled={busy || dirty || !consent || preparing} checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />
                <span>I agree to share these search details to find papers for this check.</span></label>
            </div> : <Note><strong>Offline check selected.</strong><p>Only the source articles you added will be checked. No search details will be sent anywhere.</p>{!readySources && <p>Add at least one readable source article in Advanced settings below.</p>}</Note>}
          {dirty && <Note alert>You have unsaved advanced changes. Save them below to prepare a fresh permission request.</Note>}
          <button className="button primary simple-primary check-paper" disabled={busy || !readable || dirty || (settings.mode === "online" ? !consent || !acknowledged || preparing : !readySources)} onClick={run}>
            {busy ? <LoaderCircle className="spin" size={18} /> : <Search size={18} />}Check my paper
          </button>
          <p className="simple-reassurance">Matching words are a reason to review, not a verdict of plagiarism.</p>
        </section>}

    <details ref={advanced} className="simple-advanced">
      <summary>Advanced settings <ChevronDown size={16} /></summary>
      <p className="muted">Optional. You do not need to change anything here for a normal check.</p>
      <div className="advanced-grid">
        <label>How to check<select aria-label="How to check" value={settings.mode} disabled={locked} onChange={(event) => { resetConsent(); setSettings((current) => ({ ...current, mode: event.target.value as Settings["mode"] })); }}>
          <option value="online">Find related papers online</option><option value="offline">Only compare files I add (offline)</option>
        </select></label>
        <label>Search provider<select aria-label="Search provider" value={settings.provider} disabled={locked} onChange={(event) => { resetConsent(); setSettings((current) => ({ ...current, provider: event.target.value })); }}>
          {!providers.length && <option value="crossref">Crossref + OpenAlex OA</option>}
          {providers.map((provider) => <option key={provider.id} value={provider.id} disabled={!provider.configured}>{provider.id === "crossref" ? "Crossref + OpenAlex OA" : provider.label}{!provider.configured ? " (unavailable)" : ""}</option>)}
        </select></label>
        <label>Related-paper target<input aria-label="Related-paper target" type="number" min={100} max={200} value={Number.isNaN(settings.target) ? "" : settings.target} disabled={locked} onChange={(event) => edit({ target: event.target.valueAsNumber })} /></label>
      </div>
      <p className="small muted">Referenced papers are added beyond the target. Crossref follow-up sends only public DOI identifiers to OpenAlex, up to 50 at a time—not titles. Both destinations require fresh consent. OpenAlex can also be used directly without a required key. Google Scholar is not scraped.</p>
      <section className="advanced-section"><h3>Search terms</h3>{settings.queries.map((query, index) => <div className="advanced-row" key={index}>
        <input aria-label={`Search term ${index + 1}`} value={query} disabled={locked} onChange={(event) => edit({ queries: settings.queries.map((item, i) => i === index ? event.target.value : item) })} />
        <button className="icon-button" disabled={locked} aria-label={`Remove search term ${index + 1}`} onClick={() => edit({ queries: settings.queries.filter((_, i) => i !== index) })}><X size={16} /></button>
      </div>)}<button className="button secondary small-button" disabled={locked} onClick={() => edit({ queries: [...settings.queries, ""] })}><Plus size={14} />Add search term</button></section>
      <section className="advanced-section"><h3>Correct or add references</h3>{settings.references.map((reference, index) => <div className="advanced-reference" key={reference.id}>
        <label>Reference {index + 1}<textarea aria-label={`Reference ${index + 1}`} rows={2} value={reference.raw} disabled={locked} onChange={(event) => edit({ references: settings.references.map((item) => item.id === reference.id ? { ...item, raw: event.target.value } : item) })} /></label>
        <div className="advanced-row"><label>DOI<input aria-label={`Reference ${index + 1} DOI`} value={reference.doi || ""} disabled={locked} onChange={(event) => edit({ references: settings.references.map((item) => item.id === reference.id ? { ...item, doi: event.target.value } : item) })} /></label>
          <label>Title<input aria-label={`Reference ${index + 1} title`} value={reference.title || ""} disabled={locked} onChange={(event) => edit({ references: settings.references.map((item) => item.id === reference.id ? { ...item, title: event.target.value } : item) })} /></label>
          <button className="icon-button" disabled={locked} aria-label={`Remove reference ${index + 1}`} onClick={() => edit({ references: settings.references.filter((item) => item.id !== reference.id) })}><X size={16} /></button></div>
      </div>)}<button className="button secondary small-button" disabled={locked} onClick={() => edit({ references: [...settings.references, { id: crypto.randomUUID(), raw: "" }] })}><Plus size={14} />Add reference</button></section>
      <button className="button secondary" disabled={locked || !dirty} onClick={() => void act(save)}><Check size={16} />Save advanced changes</button>
      <LocalSources job={job} locked={locked} onUpload={(files, paperId) => void act(async () => {
        for (const file of files) {
          if (!/\.(pdf|txt)$/i.test(file.name)) throw new Error("Source articles must be PDF or TXT files.");
          const body = new FormData(); body.append("file", file); if (paperId) body.append("paper_id", paperId);
          update(await api<Job>(`/jobs/${job.id}/sources`, { method: "POST", body }));
        }
        resetConsent(); setRevision((value) => value + 1);
      })} />
      <details className="advanced-extraction"><summary>Read extracted text and technical notes</summary><pre>{documentText || "No readable text extracted."}</pre>
        {!!job.warnings?.length && <ul>{job.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
      </details>
    </details>
  </div>;
}

function Progress({ job, busy, onCancel }: { job: Job; busy: boolean; onCancel: () => void }) {
  const stages = ["discovery", "acquisition", "comparison", "report"];
  const current = stages.indexOf(job.stage);
  return <section className="simple-progress">
    <div className="working-symbol"><LoaderCircle size={36} className="spin" /></div>
    <h1>{humanStages[job.stage] || "Getting your check ready"}</h1>
    <p className="simple-intro">You can leave and come back. Your progress is saved here.</p>
    <progress value={Math.max(0, Math.min(100, job.progress || 0))} max={100} aria-label="Check progress" />
    <ol className="human-stages">{stages.map((stage, index) => <li key={stage} className={index === current ? "current" : index < current ? "finished" : ""}>
      {index < current ? <Check size={17} /> : index === current ? <LoaderCircle className="spin" size={17} /> : <span className="stage-circle" />}{humanStages[stage]}
    </li>)}</ol>
    <p role="status" className="visually-hidden">{humanStages[job.stage] || "Preparing check"}: {job.progress || 0}%</p>
    <button className="button secondary" disabled={busy} onClick={onCancel}>Stop this check</button>
  </section>;
}

function LocalSources({ job, locked, onUpload }: { job: Job; locked: boolean; onUpload: (files: File[], paperId?: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const replacement = useRef<HTMLInputElement>(null);
  const [paperId, setPaperId] = useState("");
  return <section className="advanced-section local-sources"><h3>Add source articles yourself</h3><p className="small muted">Optional PDF or TXT full texts you have permission to use. Multiple files can be added together.</p>
    <button className="button secondary" disabled={locked} onClick={() => input.current?.click()}><Upload size={16} />Add source articles</button>
    <input ref={input} hidden type="file" multiple accept=".pdf,.txt" aria-label="Source article files" disabled={locked}
      onChange={(event) => { const files = Array.from(event.target.files || []); if (files.length) onUpload(files); event.target.value = ""; }} />
    {!!job.papers?.length && <ul className="local-source-list">{job.papers.map((paper) => <li key={paper.id}><div><strong>{paper.title || "Untitled article"}</strong><small>{paper.status}{paper.error ? ` · ${paper.error}` : ""}</small></div>
      {["unavailable", "discovered", "resolved", "excluded"].includes(paper.status) && <button className="button secondary small-button" disabled={locked} onClick={() => { setPaperId(paper.id); replacement.current?.click(); }}>Add its full text</button>}</li>)}</ul>}
    <input ref={replacement} hidden type="file" accept=".pdf,.txt" aria-label="Replacement article file" onChange={(event) => { if (event.target.files?.[0]) onUpload([event.target.files[0]], paperId); event.target.value = ""; }} />
  </section>;
}

function SimpleResults({ job, onRecover }: { job: Job; onRecover: () => void }) {
  const [report, setReport] = useState<Report | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [shown, setShown] = useState(10);
  useEffect(() => {
    let stale = false;
    setError("");
    api<Report>(`/jobs/${job.id}/report`).then((value) => { if (!stale) setReport(value); }).catch((err: Error) => { if (!stale) setError(err.message); });
    return () => { stale = true; };
  }, [job.id, job.updated_at, retry]);
  if (error) return <Note alert><p>{error}</p><button className="button secondary" onClick={() => setRetry((value) => value + 1)}>Try opening results again</button></Note>;
  if (!report) return <div className="simple-loading" role="status"><LoaderCircle className="spin" size={24} /><p>Opening your results…</p></div>;
  const papers = report.papers || job.papers || [];
  const compared = papers.filter((paper) => comparisonState(paper, report.source_coverage) === "full");
  const partial = papers.filter((paper) => comparisonState(paper, report.source_coverage) === "partial");
  const unexamined = papers.filter((paper) => comparisonState(paper, report.source_coverage) === "unexamined");
  const notChecked = partial.length + unexamined.length;
  const completeEnough = report.score_available !== false && compared.length > 0 && (report.coverage?.compared ?? job.coverage?.compared) !== 0;
  return <section className="simple-results">
    <div className="simple-eyebrow">{completeEnough ? "Your results" : "Your check needs another step"}</div>
    <div className="simple-results-heading"><h1>{compared.length === 0 ? "We couldn’t compare any papers yet" : "Here’s what we found."}</h1>
      <a className="button secondary" href={`/api/jobs/${job.id}/report.html`} download><ArrowDownToLine size={16} />Download report</a></div>
    <div className="simple-counts"><div><strong>{compared.length}</strong><span>papers compared</span></div><div><strong>{notChecked}</strong><span>could not be fully checked</span></div></div>
    {!completeEnough && <div className="simple-incomplete" role="alert"><CircleAlert size={23} /><div>
      <h2>{partial.length ? `${partial.length} ${partial.length === 1 ? "paper was" : "papers were"} only partly checked.` : "There isn’t enough full text to finish this check."}</h2>
      <p>{report.screening_summary || "Finding a paper is not the same as being able to read it. We need readable, permitted full-text articles to compare your sentences."}</p>
      <p>This is not a result of “no matching text.” Your paper has not been fully checked.</p>
    </div></div>}
    {(notChecked > 0 || !completeEnough) && <details className="simple-source-details unchecked-papers" open={!completeEnough}>
      <summary>Why couldn’t {notChecked || "any"} {notChecked === 1 ? "paper" : "papers"} be fully checked? <ChevronDown size={15} /></summary>
      {partial.length > 0 && <div className="partly-checked"><h3>Only partly checked</h3>{partial.map((paper) => <SourceNote key={paper.id} paper={paper} reason={sourceReason(paper, report.source_coverage)} />)}</div>}
      {unexamined.length > 0 ? unexamined.map((paper) => <SourceNote key={paper.id} paper={paper} reason={sourceReason(paper, report.source_coverage)} />)
        : !partial.length && <p>No comparison articles were available. Allow a new search below so we can look for accessible full texts.</p>}
    </details>}
    {!completeEnough ? <button className="button primary simple-primary recover-check" onClick={onRecover}>Try finding papers again <ArrowRight size={17} /></button>
      : notChecked > 0 && <button className="button secondary recover-check" onClick={onRecover}>Try checking the remaining papers <ArrowRight size={15} /></button>}
    {compared.length > 0 && <details className="simple-source-details checked-papers"><summary>See all {compared.length} papers we checked <ChevronDown size={15} /></summary>
      {compared.map((paper) => <SourceNote key={paper.id} paper={paper} reason={report.matches.some((match) => match.source_id === paper.id) ? "Matching wording to review below." : "No matching passages found in this paper."} />)}
    </details>}
    {report.matches.length > 0 ? <section className="simple-evidence">
      <h2>{report.matches.length} {report.matches.length === 1 ? "passage needs" : "passages need"} a closer look</h2>
      <p>Highlighted words match. Read both sentences and consider how the source is credited. A citation alone does not settle whether wording is appropriate.</p>
      {report.matches.slice(0, shown).map((match, index) => {
        const source = papers.find((paper) => paper.id === match.source_id);
        return <details className="sentence-match" key={index} open={index === 0}><summary><span><small>Passage {index + 1} · {reuseCategory(match.kind)}</small><strong>{source?.title || "Matching article"}</strong></span><ChevronDown size={18} /></summary>
          <div className="simple-evidence-pair"><Sentence label="Your sentence" segment={match.manuscript} ranges={match.manuscript.highlights || match.manuscript_highlights} /><Sentence label="Matching source sentence" segment={match.source} ranges={match.source.highlights || match.source_highlights} /></div>
          {source && <div className="evidence-source"><SourceLink paper={source} /></div>}
          {!!match.flags?.length && <p className="simple-match-flags">Review notes: {match.flags.map((flag) => flag.replaceAll("_", " ")).join(" · ")}</p>}
        </details>;
      })}
      {shown < report.matches.length && <button className="button secondary" onClick={() => setShown((value) => value + 10)}>Show more passages</button>}
    </section> : completeEnough ? <div className="simple-no-matches"><Check size={22} /><div><h2>No matching passages found in the papers we checked.</h2><p>This covers only the {compared.length} accessible papers listed above—not every publication, and not paraphrased ideas.</p></div></div> : null}
    <p className="simple-limitations">Paper Overlap Detector finds the same or nearly identical wording—not shared ideas or paraphrasing. It does not decide whether plagiarism occurred. Abstracts are not counted as full-text evidence.</p>
    <details className="report-technical"><summary>Detailed report information</summary>
      <dl><dt>Overlap measure</dt><dd>{completeEnough ? `${report.metrics.overlap_percent.toFixed(1)}% of eligible words` : "Not assessed—insufficient completed comparison coverage"}</dd>
        <dt>Fully compared</dt><dd>{compared.length}</dd><dt>Partly compared</dt><dd>{partial.length}</dd></dl>
      {!!report.warnings?.length && <ul>{report.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>}
      <pre>{typeof report.methodology === "string" ? report.methodology : JSON.stringify(report.methodology, null, 2)}</pre>
      <a className="button secondary" href={`/api/jobs/${job.id}/report.json`} download>Download JSON data</a>
    </details>
  </section>;
}

function SourceNote({ paper, reason }: { paper: Paper; reason: string }) {
  return <article className="simple-source-note"><h3>{paper.title || "Untitled article"}</h3><p>{reason}</p><SourceLink paper={paper} /></article>;
}

function SourceLink({ paper }: { paper: Paper }) {
  const url = safeUrl(paper.url) || safeUrl(paper.doi ? `https://doi.org/${paper.doi}` : undefined);
  return url ? <a href={url} target="_blank" rel="noreferrer">{paper.doi ? `DOI: ${paper.doi}` : "Open original article"} <ArrowRight size={12} /></a> : null;
}

function Sentence({ label, segment, ranges }: { label: string; segment: Segment; ranges?: [number, number][] }) {
  return <article className="simple-sentence"><h3>{label}</h3><p className="sentence-location">{[segment.page != null ? `Page ${segment.page}` : "", segment.section].filter(Boolean).join(" · ")}</p>
    <blockquote>{highlightedFragments(segment.text, ranges).map((part, index) => part.highlighted ? <mark key={index}>{part.text}</mark> : <span key={index}>{part.text}</span>)}</blockquote>
  </article>;
}
