import { useEffect, useRef, useState } from "react";
import "./library.css";

type Paper = { sha256: string; title: string; attribution: string; license: string; license_url: string; version: string };
type Job = { id: string; status: string; checked?: number; total?: number; overlap_percent?: number; score_available?: boolean; partial?: boolean; warnings?: string[]; error?: string };
const base = (import.meta.env.VITE_PUBLIC_API_URL || "").replace(/\/$/, "");
const gatedMode = import.meta.env.VITE_EMAIL_GATE === "true";

export default function PublicApp() {
  const [papers, setPapers] = useState<Paper[]>([]);
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
  const token = useRef("");
  const urls = useRef<string[]>([]);

  async function request(path: string, options: RequestInit = {}) {
    const response = await fetch(`${base}/api/public${path}`, {
      ...options, signal: AbortSignal.timeout(90000), credentials: "omit", headers: { ...options.headers, ...(token.current ? { Authorization: `Bearer ${token.current}` } : {}) },
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) {
        setEntered(false);
        token.current = "";
        setJob(null);
        setPapers([]); setSelected([]);
      } else if (response.status === 404 && path.startsWith("/jobs/")) {
        setJob(null);
      }
      throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status}).`);
    }
    return response;
  }
  async function loadLibrary() {
    setLoadingLibrary(true); setError("");
    try {
      const response = gatedMode ? await request("/library") : await fetch(`${base}/api/public/library`, { credentials: "omit", signal: AbortSignal.timeout(90000) });
      if (!response.ok) throw new Error("Public comparison library is unavailable. Please retry.");
      const data = await response.json();
      setPapers(data.papers); setSelected(data.papers.map((p: Paper) => p.sha256));
    } catch (e) { setError(e instanceof Error ? e.message : "The service could not be reached. Retry shortly."); }
    finally { setLoadingLibrary(false); }
  }
  useEffect(() => {
    // Old browser references never transfer Microsoft-owned jobs into a new visitor session.
    sessionStorage.removeItem("paper-overlap-public-session");
    sessionStorage.removeItem("paper-overlap-public-job");
    if (gatedMode) {
      void (async () => {
        const response = await fetch(`${base}/api/auth/config`, { credentials: "omit", signal: AbortSignal.timeout(90000) });
        if (!response.ok) throw new Error("The service could not be reached. Retry shortly.");
        const config = await response.json();
        if (config.mode !== "email-gate") throw new Error("The email access gate is not available yet.");
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

  async function compare() {
    if (!target || !consent) return;
    setBusy(true); setError("");
    try {
      if (!gatedMode && !token.current) {
        const data = await (await request("/session", { method: "POST" })).json();
        token.current = data.token;
      }
      const form = new FormData();
      form.append("target", target); form.append("selected", JSON.stringify(selected));
      sources.forEach(file => form.append("sources", file));
      const created = await (await request("/jobs", { method: "POST", body: form })).json();
      setJob(created);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function enter() {
    if (!gateReady || entering) return;
    setError(""); setEntering(true);
    try {
      const response = await request("/session", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }) });
      const data = await response.json();
      token.current = data.token; setEmail(""); setEntered(true);
      await loadLibrary();
    }
    catch (e) { setError((e as Error).message); }
    finally { setEntering(false); }
  }
  function leave() {
    token.current = ""; setEntered(false);
    urls.current.forEach(URL.revokeObjectURL); urls.current = [];
    setJob(null); setPapers([]); setSelected([]);
    setTarget(null); setSources([]); setConsent(false);
  }
  async function download(format: "pdf" | "json") {
    if (!job) return;
    setError("");
    try {
      const blob = await (await request(`/jobs/${job.id}/report.${format}`)).blob();
      const url = URL.createObjectURL(blob); urls.current.push(url);
      const link = document.createElement("a"); link.href = url; link.download = `paper-overlap-report.${format}`; link.click();
    } catch (e) { setError((e as Error).message); }
  }
  function credits() {
    const url = URL.createObjectURL(new Blob([JSON.stringify({ papers }, null, 2)], { type: "application/json" }));
    urls.current.push(url);
    const link = document.createElement("a"); link.href = url; link.download = "public-source-attributions.json"; link.click();
  }
  async function remove() {
    if (!job) return;
    try {
      const value = await (await request(`/jobs/${job.id}`, { method: "DELETE" })).json();
      setJob(value.status === "deleted" ? null : { ...job, status: value.status });
    } catch (e) { setError((e as Error).message); }
  }
  return <div className="app-shell">
    <header className="app-header"><strong>Paper Overlap Detector</strong><span>{gatedMode ? "Email access gate" : "Public preview"}</span>{gatedMode && entered && <button onClick={leave}>Leave workspace</button>}</header>
    <main className="public-main">
      <h1>Compare your paper</h1>
      <p>Compare wording against the approved server-side library or your own comparison files. No plagiarism verdict or Crossref endorsement.</p>
      <p className="reader-notice"><strong>Temporary online workspace.</strong> Uploads are sent to this service’s Azure backend, not external AI or discovery providers. Each visit has a separate private workspace, kept for up to one hour and possibly lost earlier on restart or scale-down. Download reports before refreshing or leaving: the access token is held only in this page’s memory. Do not upload confidential or sensitive manuscripts. Comparison source files are not downloadable; reports contain matched passages. No persistent personal library or DOI importing.</p>
      {error && <p role="alert" className="app-error">{error}</p>}
      {gatedMode && !entered && <section><h2>Enter your email</h2><p>Email access gate; email ownership is not verified. Anyone who knows an allowed email can enter.</p><label>Email<input type="email" maxLength={254} autoComplete="email" value={email} onChange={e => setEmail(e.target.value)} /></label><button className="primary" disabled={!gateReady || !email.trim() || entering} onClick={enter}>{entering ? "Continuing…" : "Continue"}</button></section>}
      {loadingLibrary && <p role="status">Starting the comparison service… This can take a moment after inactivity.</p>}
      {!loadingLibrary && papers.length === 0 && (!gatedMode || entered) && <button onClick={loadLibrary}>Retry connection</button>}
      {!job && (!gatedMode || entered) && <>
        <div className="public-uploads">
          <label>Your paper<input type="file" accept=".pdf,.txt" onChange={e => setTarget(e.target.files?.[0] || null)} /></label>
          <label>Your comparison papers (optional)<input type="file" multiple accept=".pdf,.txt" onChange={e => setSources(Array.from(e.target.files || []))} /></label>
        </div>
        <p>PDF or text. Manuscript: 10 MiB / 250 pages / 250,000 characters. Up to five personal sources, 8 MiB each. Total request: 32 MiB. One comparison runs at a time; daily service quotas apply.</p>
        <details><summary>Default papers: {selected.length} of {papers.length} selected</summary>
          {papers.map(p => <label className="public-paper" key={p.sha256}>
            <input type="checkbox" checked={selected.includes(p.sha256)} onChange={e => setSelected(e.target.checked ? [...selected, p.sha256] : selected.filter(id => id !== p.sha256))} />
            <span>{p.title}<small>{p.version}</small></span>
          </label>)}
        </details>
        <label className="public-consent"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} /> I may upload these documents and understand the online processing and temporary retention described above.</label>
        <button className="primary" disabled={!target || !consent || (!selected.length && !sources.length) || busy} onClick={compare}>{busy ? "Uploading…" : "Compare papers"}</button>
      </>}
      {job && (!gatedMode || entered) && <section aria-live="polite">
        <h2>{job.status === "complete" ? "Your report is ready" : job.status === "running" ? "Comparing your paper…" : "Comparison stopped"}</h2>
        {job.status === "complete" && <>
          <p>{job.checked}/{job.total} papers fully checked. {job.partial ? "Partial results; overlap is a lower bound. " : ""}{job.score_available === false ? "No score available." : `${job.overlap_percent}% text overlap.`}</p>
          <button className="primary" onClick={() => download("pdf")}>Download PDF</button>{" "}
          <button onClick={() => download("json")}>Evidence JSON</button>
          <p>For readable side comments, open the PDF’s Comments panel in Adobe Acrobat Reader. Browser popup support varies.</p>
          <details><summary>Method and limitations</summary>{job.warnings?.map((w, i) => <p key={i}>{w}</p>)}</details>
        </>}
        {job.error && <p role="alert">{job.error}</p>}
        {error && job.status === "running" && <button onClick={() => setRetryStatus(value => value + 1)}>Retry status</button>}
        <button onClick={remove}>{job.status === "running" ? "Cancel comparison" : "Delete temporary comparison"}</button>
      </section>}
      {(!gatedMode || entered) && <details><summary>Library attribution and licenses</summary>
        <button onClick={credits}>Download source credits</button>
        {papers.map(p => <section key={p.sha256}><h3>{p.title}</h3><p>{p.attribution}</p><a href={p.license_url} target="_blank" rel="noreferrer">{p.license}</a><p>{p.version}</p></section>)}
      </details>}
    </main>
    <footer>Paper Overlap Detector · Lexical similarity for human review · Not affiliated with Crossref or Turnitin · <a href="https://github.com/OmerHanan1/buna-screening-tool" target="_blank" rel="noreferrer">Source code (AGPL-3.0-or-later)</a></footer>
  </div>;
}
