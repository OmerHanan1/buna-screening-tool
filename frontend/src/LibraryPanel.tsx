import { useEffect, useState } from "react";
import { api, json } from "./api";
import type { LocalJob } from "./ManualApp";
import "./library.css";
import type { CollectionList } from "./collections";

interface LibraryPaper {
  id: string; doi: string | null; title: string; status: string; reason: string;
  authors: string[]; year: number | null; parser_status: string; updated_at: string;
  errors: { stage: string; provider?: string; reason: string; url?: string }[];
  provenance: { provider: string; license?: string; version?: string }[];
  oa?: { openalex_record?: boolean; openalex_is_oa?: boolean; openalex_oa_status?: string;
    checked?: string[]; europepmc?: { record?: boolean; pmcid?: string; open_access_subset?: boolean; free_fulltext?: boolean } };
}
interface LibraryPage { papers: LibraryPaper[]; total: number; offset: number; limit: number; active: number }
interface Preview {
  dois: string[]; duplicates: string[];
  invalid: { position: number; value: string; reason: string }[];
}
interface Config { disclosure: string; disclosure_version: number }
interface Batch { id: string; paper_ids: string[]; total: number; finished: number; papers: LibraryPaper[] }
const activeStates = ["queued", "resolving", "downloading", "processing"];

export default function LibraryPanel({ job, locked, onAttached, onClose }: {
  job: LocalJob | null; locked: boolean; onAttached: (job: LocalJob) => void; onClose: () => void;
}) {
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [config, setConfig] = useState<Config | null>(null);
  const [page, setPage] = useState<LibraryPage | null>(null);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [uploadDoi, setUploadDoi] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [retryPaper, setRetryPaper] = useState<LibraryPaper | null>(null);
  const [collections, setCollections] = useState<CollectionList | null>(null);
  const [request, setRequest] = useState({ text: "", key: crypto.randomUUID() });
  const canAttach = Boolean(job && !locked && !job.report_available);
  useEffect(() => {
    const controller = new AbortController();
    api<Config>("/library/config", { signal: controller.signal }).then(setConfig)
      .catch(e => { if (!controller.signal.aborted) setError((e as Error).message); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    setPreview(null);
    const timer = window.setTimeout(() => {
      api<Preview>("/library/preview", { ...json("POST", { text }), signal: controller.signal }).then(setPreview)
        .catch(e => { if (!controller.signal.aborted) setError((e as Error).message); });
    }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [text]);
  useEffect(() => {
    const controller = new AbortController();
    let timer: number;
    async function load() {
      try {
        const value = await api<LibraryPage>(`/library/papers?q=${encodeURIComponent(query)}&offset=${offset}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setPage(value);
        const collectionList = await api<CollectionList>("/library/collections", { signal: controller.signal });
        if (!controller.signal.aborted) setCollections(collectionList);
        if (batch?.id) {
          const result = await api<Batch>(`/library/imports/${batch.id}`, { signal: controller.signal });
          if (!controller.signal.aborted) setBatch(result);
        }
        if (!controller.signal.aborted) timer = window.setTimeout(() => void load(), value.active ? 750 : 2500);
      } catch (e) {
        if (!controller.signal.aborted) {
          setError(`Library refresh failed: ${(e as Error).message}`);
          timer = window.setTimeout(() => void load(), 3000);
        }
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [query, offset, batch?.id, refresh]);
  useEffect(() => { setSelected([]); }, [job?.id]);

  async function perform(message: string, action: () => Promise<void>) {
    setBusy(message); setError(""); setNotice("");
    try { await action(); setRefresh(value => value + 1); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(""); }
  }
  return <section className="library-panel" aria-label="Paper library">
    <div className="section-heading"><div><h2>Paper library</h2><p className="muted">Your reusable local papers dataset</p></div>
      <button onClick={onClose} disabled={Boolean(busy)}>Close library</button></div>
    {collections?.collections.map(collection => <details className="library-local" key={collection.id}>
      <summary>{collection.name} · Ready {collection.ready_references} / {collection.total_references} reference entries · {collection.ready_unique} distinct papers</summary>
      <p>Reference numbers are report entries, not library paper IDs. Aliases share a paper.
        Missing or uncertain sources are never selected by default. Upload a lawful correct file and have its identity verified before updating this collection.</p>
      <ol className="library-papers">{collection.entries.map(entry => <li key={entry.reference_id}>
        <strong>Reference {entry.reference_id}: {entry.title}</strong>
        <small>{entry.comparison_ready ? "Ready" : entry.status} · {entry.reason}</small>
        <small>{entry.version} · {entry.license} · {entry.access_basis}</small>
      </li>)}</ol>
    </details>)}
    <div className="library-import">
      <label>DOIs<textarea aria-label="DOIs" rows={4} value={text} placeholder="10.1234/example or https://doi.org/10.1234/example"
        disabled={Boolean(busy)} onChange={event => { setText(event.target.value); setPreview(null); setError(""); }} /></label>
      <p className="muted">One or several DOIs, separated by lines or spaces. Up to 200 at once. Punctuation is never silently stripped.</p>
      {preview && Boolean(text.trim()) && <div aria-label="DOI preview">
        <strong>{preview.dois.length} unique DOI{preview.dois.length === 1 ? "" : "s"}</strong>
        {Boolean(preview.duplicates.length) && <p>{preview.duplicates.length} duplicate entries will be reused.</p>}
        <ul className="library-dois">{preview.dois.map(doi => <li key={doi}>{doi}</li>)}</ul>
        {preview.invalid.map(item => <p role="alert" key={item.position}>Entry {item.position}: {item.value} — {item.reason}</p>)}
      </div>}
      <p className="library-disclosure">{config?.disclosure || "Loading import disclosure…"}</p>
      <button className="primary" disabled={!config || !preview?.dois.length || Boolean(preview.invalid.length) || Boolean(busy)}
        onClick={() => void perform("Starting DOI import…", async () => {
          if (!config) return;
          const key = request.text === text ? request.key : crypto.randomUUID();
          setRequest({ text, key });
          const result = await api<{ id: string; paper_ids: string[] }>("/library/imports", json("POST", {
            text, request_key: key, disclosure_version: config.disclosure_version,
          }));
          setBatch(await api<Batch>(`/library/imports/${result.id}`));
          setNotice("Import started. Existing entries are reused; use Retry for unsuccessful entries.");
        })}>Import</button>
    </div>
    {batch && <div className="library-progress" aria-live="polite">
      <strong>Import: {batch.finished} / {batch.total} finished</strong>
      <progress aria-label="Import progress" value={batch.finished} max={batch.total || 1} />
      <span>{batch.papers.filter(p => p.status === "ready").length} ready for comparison</span>
      {batch.finished < batch.total && <button disabled={Boolean(busy)} onClick={() => void perform("Cancelling import…", async () => {
        for (const paper of batch.papers.filter(p => activeStates.includes(p.status))) {
          await api(`/library/papers/${paper.id}/cancel`, { method: "POST" });
        }
        setNotice("Cancellation requested. Shared DOI entries are cancelled in the library, not removed.");
      })}>Cancel import</button>}
    </div>}
    {retryPaper && <section className="library-disclosure" aria-label="Retry import confirmation">
      <h3>Retry this DOI?</h3><p><strong>{retryPaper.doi}</strong></p>
      <p>{config?.disclosure || "Loading current import disclosure…"}</p>
      <p>Only this entry is retried. Existing comparisons are not changed or rerun.</p>
      <button disabled={!config || Boolean(busy)} className="primary" onClick={() => void perform("Retrying this DOI…", async () => {
        if (!config) return;
        await api(`/library/papers/${retryPaper.id}/retry`, json("POST", { disclosure_version: config.disclosure_version }));
        setRetryPaper(null);
      })}>Approve and retry this DOI</button>
      <button disabled={Boolean(busy)} onClick={() => setRetryPaper(null)}>Cancel retry</button>
    </section>}
    <details className="library-local"><summary>Save local files to the library</summary>
      <p>PDF or UTF-8 text, up to 32 MiB, 600 PDF pages and 2 million extracted characters per source. No network requests. Scanned PDFs need OCR first. Ready means parsed, not fully compared.</p>
      <label>Optional DOI for a single file<input aria-label="Local file DOI" value={uploadDoi} onChange={event => setUploadDoi(event.target.value)} /></label>
      <label>Save local papers<input aria-label="Save local papers" type="file" multiple accept=".pdf,.txt" disabled={Boolean(busy)}
        onChange={event => {
          const files = Array.from(event.target.files || []); event.target.value = "";
          if (!files.length) return;
          void perform("Saving local papers…", async () => {
            if (uploadDoi && files.length !== 1) throw new Error("Supply a DOI only when saving one file.");
            const failures: string[] = [];
            let saved = 0;
            for (const file of files) {
              try {
                const body = new FormData(); body.append("file", file); body.append("doi", uploadDoi);
                await api("/library/uploads", { method: "POST", body }); saved++;
              } catch (e) { failures.push(`${file.name}: ${(e as Error).message}`); }
            }
            setNotice(`${saved} local paper${saved === 1 ? "" : "s"} saved.`);
            if (failures.length) { setRefresh(value => value + 1); throw new Error(failures.join("\n")); }
          });
        }} /></label>
    </details>
    <div className="library-toolbar">
      <label>Search library<input aria-label="Search library" value={query} onChange={event => { setQuery(event.target.value); setOffset(0); }} /></label>
      <span>{page?.total ?? 0} saved entries</span>
    </div>
    {error && <p className="app-error" role="alert">{error}</p>}
    {(busy || notice) && <p role="status">{busy || notice}</p>}
    <ul className="library-papers">{page?.papers.map(paper => <li key={paper.id}>
      <label className="library-choice">
        <input type="checkbox" aria-label={`Select ${paper.title}`} disabled={paper.status !== "ready" || Boolean(busy)}
          checked={selected.includes(paper.id)} onChange={event => setSelected(current =>
            event.target.checked ? [...current, paper.id] : current.filter(id => id !== paper.id))} />
        <span><strong>{paper.title}</strong><small>{[paper.doi, paper.year].filter(Boolean).join(" · ")}</small>
          <small className={paper.status === "ready" ? "library-ready" : ""}>{paper.status} · {paper.reason}</small></span>
      </label>
      <div className="library-row-actions">
        {activeStates.includes(paper.status) && <button disabled={Boolean(busy)}
          onClick={() => void perform("Cancelling…", async () => { await api(`/library/papers/${paper.id}/cancel`, { method: "POST" }); })}>Cancel</button>}
        {paper.doi && ["failed", "metadata-only", "unavailable", "cancelled"].includes(paper.status) && <button disabled={!config || Boolean(busy)}
          onClick={() => setRetryPaper(paper)}>Retry</button>}
      </div>
      {(paper.errors.length > 0 || paper.provenance.length > 0) && <details><summary>Source details{paper.errors.length ? ` · ${paper.errors.length} notices` : ""}</summary>
        <p>Parser: {paper.parser_status}</p>
        {paper.oa ? <p>Checked: {paper.oa.checked?.join(", ") || "Not recorded"}.
          OpenAlex: {paper.oa.openalex_record === false ? "no record" : paper.oa.openalex_oa_status || "status unknown"}.
          {paper.oa.europepmc?.pmcid ? ` Europe PMC: ${paper.oa.europepmc.pmcid}; ${paper.oa.europepmc.open_access_subset ? "OA API subset" : "not in OA API subset"}.` : ""}
          These are provider metadata, not a verified paywall determination.</p> :
          <p>Detailed OA status was not recorded for this earlier import. An explicitly approved retry can obtain current metadata.</p>}
        {paper.provenance.map((source, i) => <p key={i}>{[source.provider, source.version, source.license].filter(Boolean).join(" · ")}</p>)}
        {paper.errors.map((error, i) => <p key={i}>{error.provider} {error.stage}: {error.reason}{error.url && <small>{error.url}</small>}</p>)}
      </details>}
    </li>)}</ul>
    {page?.total === 0 && <p>No papers found. Import DOIs or save local files to begin.</p>}
    <div className="library-pagination">
      <button disabled={!offset} onClick={() => setOffset(Math.max(0, offset - 25))}>Previous</button>
      <span>{page?.total ? `${offset + 1}–${Math.min(offset + 25, page.total)} of ${page.total}` : "0 entries"}</span>
      <button disabled={!page || offset + 25 >= page.total} onClick={() => setOffset(offset + 25)}>Next</button>
    </div>
    <div className="library-attach"><p>{canAttach
      ? "Add saved papers alongside any newly uploaded comparison files. Then use Compare papers."
      : "Choose a manuscript in a new comparison to attach saved papers."}</p>
      <button className="primary" disabled={!canAttach || !selected.length || Boolean(busy)}
        onClick={() => void perform("Adding saved papers…", async () => {
          if (!job) return;
          const updated = await api<LocalJob>(`/library/comparisons/${job.id}/sources`, json("POST", { paper_ids: selected }));
          onAttached(updated); setSelected([]); setNotice("Saved papers attached. Nothing was downloaded or compared.");
        })}>Add selected to comparison{selected.length ? ` · ${selected.length}` : ""}</button>
    </div>
  </section>;
}
