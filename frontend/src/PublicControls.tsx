import { useEffect, useRef, useState } from "react";
import { Check, FileText, Search, Upload, X } from "lucide-react";

export type HostedPaper = { sha256: string; title: string; attribution: string; license: string; license_url: string; version: string; library_version?: string; storage_kind?: "bundled" | "shared" };

export function fileSize(bytes: number) {
  return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export async function validateFile(file: File, maximumMiB: number): Promise<string> {
  if (!/\.(pdf|txt)$/i.test(file.name)) return "Choose a PDF or a plain-text file.";
  if (!file.size) return "This file is empty. Choose another file.";
  if (file.size > maximumMiB * 1024 * 1024) return `This file exceeds the ${maximumMiB} MB limit.`;
  if (/\.pdf$/i.test(file.name)) {
    const header = new TextDecoder().decode(await file.slice(0, 5).arrayBuffer());
    if (header !== "%PDF-") return "This file does not contain a valid PDF header.";
  }
  return "";
}

export function ManuscriptInput({ file, error, onFile, onRemove }: {
  file: File | null; error: string; onFile: (file: File) => void; onRemove: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  return <div>
    <input ref={input} tabIndex={-1} className="hosted-hidden-input" type="file" accept=".pdf,.txt" aria-label="Your paper"
      onChange={e => { if (e.target.files?.[0]) onFile(e.target.files[0]); e.target.value = ""; }} />
    {file ? <div className="hosted-file-row">
      <FileText aria-hidden="true" size={24} />
      <div className="hosted-file-info"><strong title={file.name}>{file.name}</strong>
        <span>{file.name.split(".").pop()?.toUpperCase()} · {fileSize(file.size)} <span className="hosted-file-ready"><Check size={12} aria-hidden="true" /> Ready to upload</span></span></div>
      <button type="button" className="hosted-text-button" onClick={() => input.current?.click()}>Replace</button>
      <button type="button" className="hosted-icon-button" aria-label={`Remove ${file.name}`} onClick={onRemove}><X size={17} /></button>
    </div> : <button type="button" className={`hosted-dropzone ${dragging ? "is-dragging" : ""}`}
      onClick={() => input.current?.click()} onDragOver={e => { e.preventDefault(); setDragging(true); }}
      onDragLeave={() => setDragging(false)} onDrop={e => { e.preventDefault(); setDragging(false); if (e.dataTransfer.files[0]) onFile(e.dataTransfer.files[0]); }}>
      <Upload size={22} aria-hidden="true" /><strong>Choose your manuscript</strong><span>or drop it here · PDF or text, up to 10 MB</span>
    </button>}
    {error && <p role="alert" className="hosted-field-error">{error}</p>}
  </div>;
}

export function PaperDialog({ papers, selected, onSelect, onClose, readOnly = false, onRemove }: {
  papers: HostedPaper[]; selected: string[]; onSelect: (ids: string[]) => void; onClose: () => void; readOnly?: boolean;
  onRemove?: (paper: HostedPaper) => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [query, setQuery] = useState("");
  const [credits, setCredits] = useState(false);
  const [removing, setRemoving] = useState<HostedPaper | null>(null);
  const filtered = papers.filter(p => (p.title + " " + p.version).toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialog.current?.showModal();
    return () => { dialog.current?.close(); previous?.focus(); };
  }, []);
  return <dialog ref={dialog} className="hosted-dialog" aria-labelledby="source-dialog-title" onCancel={onClose}
    onClick={e => { if (e.target === e.currentTarget) onClose(); }}>
    <div className="hosted-dialog-header"><div><h2 id="source-dialog-title">Comparison papers</h2><p>{selected.length} of {papers.length} selected</p></div>
      <button className="hosted-icon-button" onClick={onClose} aria-label="Close comparison papers"><X size={20} /></button></div>
    <div className="hosted-dialog-tools">
      <label className="hosted-search"><Search size={16} aria-hidden="true" /><input autoFocus aria-label="Search comparison papers" placeholder="Search title or version" value={query} onChange={e => setQuery(e.target.value)} /></label>
      <div className="hosted-bulk"><button disabled={readOnly} className="hosted-text-button" onClick={() => onSelect(papers.map(p => p.sha256))}>Select all</button><button disabled={readOnly} className="hosted-text-button" onClick={() => onSelect([])}>Clear all</button><label><input type="checkbox" checked={credits} onChange={e => setCredits(e.target.checked)} /> Show credits</label></div>
    </div>
    <div className="hosted-source-list">
      {filtered.map(paper => <div className="hosted-source-item" key={paper.sha256}>
        <label><input type="checkbox" disabled={readOnly} checked={selected.includes(paper.sha256)} onChange={e => onSelect(e.target.checked ? [...selected, paper.sha256] : selected.filter(id => id !== paper.sha256))} />
          <span className="hosted-source-copy"><strong title={paper.title}>{paper.title}</strong><small title={paper.version}>{paper.version}</small></span>
        </label>
        {onRemove && paper.library_version !== undefined && <button className="hosted-text-button hosted-source-remove" aria-label={`Remove from library: ${paper.title}`} onClick={() => setRemoving(paper)}>Remove from library</button>}
        {credits && <details><summary>Attribution and rights</summary><p className="hosted-attribution">{paper.attribution}</p>{paper.license_url ? <a href={paper.license_url} target="_blank" rel="noreferrer">{paper.license}</a> : <p>{paper.license}</p>}</details>}
      </div>)}
      {!filtered.length && <p className="hosted-empty">No papers match “{query}”.</p>}
    </div>
    <div className="hosted-dialog-footer"><span>Original source files are not downloadable.</span><button className="hosted-primary" onClick={onClose}>Done</button></div>
    {removing && onRemove && <RemovalConfirmation paper={removing} onCancel={() => setRemoving(null)} onRemove={async () => { await onRemove(removing); setRemoving(null); }} />}
  </dialog>;
}

function RemovalConfirmation({ paper, onCancel, onRemove }: {
  paper: HostedPaper; onCancel: () => void; onRemove: () => Promise<void>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { dialog.current?.showModal(); cancel.current?.focus(); return () => dialog.current?.close(); }, []);
  async function confirm() {
    if (busy) return;
    setBusy(true); setError("");
    try { await onRemove(); }
    catch (e) { setError((e as Error).message); setBusy(false); }
  }
  return <dialog ref={dialog} role="alertdialog" className="hosted-dialog hosted-removal-confirmation"
    aria-labelledby="remove-library-title" aria-describedby="remove-library-description"
    onCancel={e => { e.preventDefault(); e.stopPropagation(); if (!busy) onCancel(); }}>
    <h2 id="remove-library-title">Remove for everyone?</h2>
    <p id="remove-library-description">Remove <strong>“{paper.title}”</strong> from the shared library for all app users? Existing comparisons and reports will not change. Deselecting only changes your current comparison; this action changes future library selections for everyone.</p>
    <p>{paper.storage_kind === "bundled" ? "The bundled file remains packaged privately on the backend. This removes it from the library, not from the packaged image." : "Private source files are cleaned up after a retention delay. Existing temporary comparison copies remain until they expire."}</p>
    {error && <p role="alert" className="hosted-error">{error}</p>}
    <div className="hosted-bottom-actions"><button ref={cancel} disabled={busy} onClick={onCancel}>Cancel</button><button className="hosted-primary" disabled={busy} onClick={() => void confirm()}>{busy ? "Removing…" : "Remove from library"}</button></div>
  </dialog>;
}

export function Elapsed({ since }: { since: number }) {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const update = () => setSeconds(Math.max(0, Math.floor((Date.now() - since) / 1000)));
    update(); const timer = window.setInterval(update, 1000); return () => window.clearInterval(timer);
  }, [since]);
  return <span>{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")} elapsed</span>;
}
