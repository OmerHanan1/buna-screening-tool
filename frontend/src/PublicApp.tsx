import { useEffect, useRef, useState } from "react";
import { PublicClientApplication, InteractionRequiredAuthError } from "@azure/msal-browser";
import "./library.css";

type Paper = { sha256: string; title: string; attribution: string; license: string; license_url: string; version: string };
type Job = { id: string; status: string; checked?: number; total?: number; overlap_percent?: number; score_available?: boolean; partial?: boolean; warnings?: string[]; error?: string };
const base = (import.meta.env.VITE_PUBLIC_API_URL || "").replace(/\/$/, "");
const teamMode = import.meta.env.VITE_TEAM_MODE === "true";

export default function PublicApp() {
  const [papers, setPapers] = useState<Paper[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [target, setTarget] = useState<File | null>(null);
  const [sources, setSources] = useState<File[]>([]);
  const [job, setJob] = useState<Job | null>(() => {
    const id = sessionStorage.getItem("paper-overlap-public-job");
    return id ? { id, status: "running" } : null;
  });
  const [retryStatus, setRetryStatus] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [consent, setConsent] = useState(false);
  const [loadingLibrary, setLoadingLibrary] = useState(true);
  const [signedIn, setSignedIn] = useState(false);
  const auth = useRef<{ client: PublicClientApplication; scope: string } | null>(null);
  const authInit = useRef<Promise<void> | null>(null);
  const token = useRef(sessionStorage.getItem("paper-overlap-public-session") || "");
  const urls = useRef<string[]>([]);

  async function request(path: string, options: RequestInit = {}) {
    let bearer = token.current;
    if (teamMode) {
      const current = auth.current;
      const account = current?.client.getActiveAccount();
      if (!current || !account) throw new Error("Sign in with the approved Microsoft account.");
      try {
        bearer = (await current.client.acquireTokenSilent({ scopes: [current.scope], account })).accessToken;
      } catch (e) {
        if (e instanceof InteractionRequiredAuthError) {
          setSignedIn(false);
          throw new Error("Your Microsoft session needs confirmation. Sign in again.");
        }
        throw e;
      }
    }
    const response = await fetch(`${base}/api/public${path}`, {
      ...options, signal: AbortSignal.timeout(90000), credentials: "omit", headers: { ...options.headers, ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}) },
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      if (response.status === 401) {
        if (teamMode) setSignedIn(false);
        token.current = "";
        sessionStorage.removeItem("paper-overlap-public-session");
        sessionStorage.removeItem("paper-overlap-public-job");
        setJob(null);
      } else if (response.status === 404 && path.startsWith("/jobs/")) {
        sessionStorage.removeItem("paper-overlap-public-job");
        setJob(null);
      }
      throw new Error(typeof data.detail === "string" ? data.detail : `Request failed (${response.status}).`);
    }
    return response;
  }
  async function loadLibrary() {
    setLoadingLibrary(true); setError("");
    try {
      const response = teamMode ? await request("/library") : await fetch(`${base}/api/public/library`, { credentials: "omit", signal: AbortSignal.timeout(90000) });
      if (!response.ok) throw new Error("Public comparison library is unavailable. Please retry.");
      const data = await response.json();
      setPapers(data.papers); setSelected(data.papers.map((p: Paper) => p.sha256));
    } catch (e) { setError(e instanceof Error ? e.message : "The service could not be reached. Retry shortly."); }
    finally { setLoadingLibrary(false); }
  }
  useEffect(() => {
    if (teamMode) {
      authInit.current ||= (async () => {
        const response = await fetch(`${base}/api/auth/config`, { credentials: "omit", signal: AbortSignal.timeout(90000) });
        if (!response.ok) throw new Error("Team sign-in is not available. Anonymous access is not used as a fallback.");
        const config = await response.json();
        if (config.mode !== "team") throw new Error("The service is not configured for team sign-in.");
        const client = new PublicClientApplication({
          auth: { clientId: config.client_id, authority: config.authority,
                  redirectUri: window.location.origin + import.meta.env.BASE_URL,
                  postLogoutRedirectUri: window.location.origin + import.meta.env.BASE_URL },
          cache: { cacheLocation: "sessionStorage" },
        });
        await client.initialize();
        const redirect = await client.handleRedirectPromise();
        if (redirect?.account) client.setActiveAccount(redirect.account);
        if (!client.getActiveAccount() && client.getAllAccounts().length === 1) client.setActiveAccount(client.getAllAccounts()[0]);
        auth.current = { client, scope: config.scope };
        setSignedIn(Boolean(client.getActiveAccount()));
        if (client.getActiveAccount()) await loadLibrary();
        else setLoadingLibrary(false);
      })();
      authInit.current.catch(e => { setError(e.message); setLoadingLibrary(false); });
    } else void loadLibrary();
    return () => { urls.current.forEach(URL.revokeObjectURL); };
  }, []);
  useEffect(() => {
    if (!job || job.status !== "running" || (teamMode && !signedIn)) return;
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
  }, [job?.id, job?.status, retryStatus, signedIn]);

  async function compare() {
    if (!target || !consent) return;
    setBusy(true); setError("");
    try {
      if (!teamMode && !token.current) {
        const data = await (await request("/session", { method: "POST" })).json();
        token.current = data.token; sessionStorage.setItem("paper-overlap-public-session", data.token);
      }
      const form = new FormData();
      form.append("target", target); form.append("selected", JSON.stringify(selected));
      sources.forEach(file => form.append("sources", file));
      const created = await (await request("/jobs", { method: "POST", body: form })).json();
      sessionStorage.setItem("paper-overlap-public-job", created.id);
      setJob(created);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function signIn() {
    if (!auth.current) return;
    setError("");
    try { await auth.current.client.loginRedirect({ scopes: [auth.current.scope], prompt: "select_account" }); }
    catch (e) { setError((e as Error).message); }
  }
  async function signOut() {
    sessionStorage.removeItem("paper-overlap-public-job");
    sessionStorage.removeItem("paper-overlap-public-session");
    urls.current.forEach(URL.revokeObjectURL); urls.current = [];
    setJob(null); setPapers([]); setSelected([]);
    if (auth.current) await auth.current.client.logoutRedirect();
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
      if (value.status === "deleted") sessionStorage.removeItem("paper-overlap-public-job");
      setJob(value.status === "deleted" ? null : { ...job, status: value.status });
    } catch (e) { setError((e as Error).message); }
  }
  return <div className="app-shell">
    <header className="app-header"><strong>Paper Overlap Detector</strong><span>{teamMode ? "Private team workspace" : "Public preview"}</span>{teamMode && signedIn && <button onClick={signOut}>Sign out</button>}</header>
    <main className="public-main">
      <h1>Compare your paper</h1>
      <p>Compare wording against the {teamMode ? "approved server-side" : "redistribution-reviewed"} library or your own comparison files. No plagiarism verdict or Crossref endorsement.</p>
      <p className="reader-notice"><strong>Temporary online workspace.</strong> Uploaded documents are sent to this service’s Azure backend, not external AI or discovery providers. They are private to {teamMode ? "your approved Microsoft identity" : "this browser session"}, kept for up to one hour, and may disappear earlier on restart or scale-down. Download your report promptly. Do not upload confidential or sensitive manuscripts. This preview does not provide persistent personal libraries or DOI importing.{teamMode && " Comparison source files are not downloadable. Reports contain matched excerpts only, not full comparison papers."}</p>
      {error && <p role="alert" className="app-error">{error}</p>}
      {teamMode && !signedIn && <section><h2>Sign in to compare papers</h2><p>Only explicitly approved Microsoft accounts can access the library and comparisons.</p><button className="primary" disabled={!auth.current} onClick={signIn}>Sign in with Microsoft</button></section>}
      {loadingLibrary && <p role="status">Starting the comparison service… This can take a moment after inactivity.</p>}
      {!loadingLibrary && papers.length === 0 && (!teamMode || signedIn) && <button onClick={loadLibrary}>Retry connection</button>}
      {!job && (!teamMode || signedIn) && <>
        <div className="public-uploads">
          <label>Your paper<input type="file" accept=".pdf,.txt" onChange={e => setTarget(e.target.files?.[0] || null)} /></label>
          <label>Your comparison papers (optional)<input type="file" multiple accept=".pdf,.txt" onChange={e => setSources(Array.from(e.target.files || []))} /></label>
        </div>
        <p>PDF or text. Manuscript: 10 MiB / 250 pages / 250,000 characters. Up to five personal sources, 8 MiB each. Total request: 32 MiB. One comparison runs at a time; daily service quotas apply.</p>
        <details><summary>{teamMode ? "Default papers" : "Public library"}: {selected.length} of {papers.length} selected</summary>
          {papers.map(p => <label className="public-paper" key={p.sha256}>
            <input type="checkbox" checked={selected.includes(p.sha256)} onChange={e => setSelected(e.target.checked ? [...selected, p.sha256] : selected.filter(id => id !== p.sha256))} />
            <span>{p.title}<small>{p.version}</small></span>
          </label>)}
        </details>
        <label className="public-consent"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} /> I may upload these documents and understand the online processing and temporary retention described above.</label>
        <button className="primary" disabled={!target || !consent || (!selected.length && !sources.length) || busy} onClick={compare}>{busy ? "Uploading…" : "Compare papers"}</button>
      </>}
      {job && (!teamMode || signedIn) && <section aria-live="polite">
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
      {(!teamMode || signedIn) && <details><summary>Library attribution and licenses</summary>
        <button onClick={credits}>Download source credits</button>
        {papers.map(p => <section key={p.sha256}><h3>{p.title}</h3><p>{p.attribution}</p><a href={p.license_url} target="_blank" rel="noreferrer">{p.license}</a><p>{p.version}</p></section>)}
      </details>}
    </main>
    <footer>Paper Overlap Detector · Lexical similarity for human review · Not affiliated with Crossref or Turnitin · <a href="https://github.com/OmerHanan1/buna-screening-tool" target="_blank" rel="noreferrer">Source code (AGPL-3.0-or-later)</a></footer>
  </div>;
}
