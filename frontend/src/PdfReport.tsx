import { useEffect, useState } from "react";
import type { ReaderReport } from "./reader-types";

export default function PdfReport({ report, jobId, engineVersion }: { report: ReaderReport; jobId: string; engineVersion: string }) {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [unmapped, setUnmapped] = useState(0);
  const [mode, setMode] = useState("");
  const endpoint = `/api/jobs/${jobId}/report.pdf`;
  const scope = report.settings?.manuscript_scope;
  const basis = report.metrics.score_basis === "abstract-onward-word-units" ? "words from the Abstract onward"
    : report.metrics.score_basis === "all-submitted-word-units" ? "total submitted word units" : "eligible manuscript words (saved legacy basis)";
  useEffect(() => {
    const controller = new AbortController();
    setReady(false); setError("");
    fetch(endpoint, { signal: controller.signal }).then(async response => {
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        throw new Error(typeof data?.detail === "string" ? data.detail : `PDF generation failed (${response.status}).`);
      }
      if (!response.headers.get("content-type")?.includes("application/pdf")) throw new Error("The server did not return a PDF.");
      const bytes = await response.arrayBuffer();
      if (new TextDecoder().decode(bytes.slice(0, 5)) !== "%PDF-") throw new Error("The generated file is not a valid PDF response.");
      if (controller.signal.aborted) return;
      setUnmapped(Number(response.headers.get("X-Buna-Pdf-Unmapped-Ranges") || 0));
      setMode(response.headers.get("X-Buna-Pdf-Mode") || "");
      setReady(true);
    }).catch(e => { if (!controller.signal.aborted) setError((e as Error).message); });
    return () => controller.abort();
  }, [endpoint, report.generated_at]);
  return <section className="pdf-report" aria-label="PDF similarity report">
    <header className="pdf-report-summary">
      <div><p className="eyebrow">LOCAL PDF REPORT</p><h1>Your PDF report</h1>
        <p>{report.result.complete_sources}/{report.papers.length} papers fully checked.
          {report.result.score_available ? ` ${report.metrics.overlap_percent}% text overlap.` : " No score available."}</p></div>
      <div className="reader-actions"><a className="button" href={endpoint} target="_blank" rel="noopener noreferrer">Open PDF</a>
        <a className="button primary" href={`${endpoint}?download=true`} download="paper-overlap-report.pdf">Download PDF</a></div>
    </header>
    {report.result.state !== "complete" && <p role="status" className="reader-notice">{report.result.heading}. {report.result.explanation}</p>}
    {report.comparison_model === "experimental-ordered" && <p className="reader-notice">Experimental model. Accuracy and vendor equivalence are not established.</p>}
    {scope?.requested === "abstract-onward" && scope.applied === "whole-document" &&
      <p className="reader-notice">Abstract heading not detected; the whole manuscript was analyzed (front matter was not excluded).</p>}
    <p className="pdf-caption">Highlighted sequences point to the numbered comparison papers. This is text overlap for review, not a plagiarism verdict.</p>
    {!ready && !error && <p role="status" className="pdf-loading">Generating your PDF locally…</p>}
    {error && <p role="alert" className="app-error">{error}</p>}
    {ready && <>
      {unmapped > 0 && <p className="reader-notice">{unmapped} ranges could not be placed reliably. No guessed highlights were drawn; see the PDF note and mapping details.</p>}
      {mode && mode !== "original-pdf" && <p className="pdf-caption">This PDF is typeset from the saved text, not the original PDF layout.</p>}
      <div className="pdf-ready-file" role="status"><span aria-hidden="true">PDF</span>
        <div><strong>paper-overlap-report.pdf</strong><p>Ready. {mode === "original-pdf" ? "Original manuscript pages with highlights and numbered source references." : "A clean, paginated PDF of your saved text."}</p>
          <p>For readable side comments, download the PDF and open Comments in Adobe Acrobat Reader. Source numbers identify papers in the first-page key. Browser PDF viewers may hide comments or clip popups; long passages continue in linked replies.</p></div></div>
    </>}
    <details className="pdf-details"><summary>Report details</summary>
      <p>Saved comparison: {report.algorithm_version || "legacy"} · Running engine: {engineVersion}. Original comparisons are not rerun when a PDF is generated.</p>
      <p>{report.metrics.overlapping_words} included matching words / {report.metrics.score_denominator_words ?? report.metrics.eligible_words} {basis}.
        Only your selected local comparison papers are in scope.</p>
      {scope?.applied === "abstract-onward" && <p>Analysis starts at the Abstract heading on page {scope.start_page}; {report.metrics.front_matter_words} preceding front-matter words were excluded. Original PDF pages are preserved.</p>}
      <div className="reader-actions"><a href={`/api/jobs/${jobId}/report.json`} download>Evidence JSON</a>
        <a href={`/api/jobs/${jobId}/report.pdf-mapping.json`} target="_blank" rel="noopener noreferrer">PDF mapping details</a></div>
    </details>
  </section>;
}
