import { useEffect, useRef, useState } from "react";
import { Download, ExternalLink } from "lucide-react";
import { readPdf } from "./pdf-transfer";

export default function PdfActions({ jobId, load }: {
  jobId: string; load: (signal: AbortSignal) => Promise<Response>;
}) {
  const loader = useRef(load);
  loader.current = load;
  const [attempt, setAttempt] = useState(0);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [bytes, setBytes] = useState(0);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    controller.current = abort;
    let disposed = false, objectUrl = "", timedOut = false;
    setUrl(""); setError(""); setBytes(0);
    const timeout = window.setTimeout(() => { timedOut = true; abort.abort(); }, 60000);
    void (async () => {
      try {
        const response = await loader.current(abort.signal);
        const blob = await readPdf(response, count => { if (!disposed) setBytes(count); });
        if (disposed) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      } catch (e) {
        if (disposed) return;
        setError(timedOut ? "The PDF transfer timed out. Your comparison is still saved; retry the PDF transfer."
          : abort.signal.aborted ? "PDF transfer stopped. You can retry without rerunning the comparison."
          : e instanceof Error ? e.message : "The PDF transfer failed. Please retry.");
      } finally { window.clearTimeout(timeout); }
    })();
    return () => { disposed = true; window.clearTimeout(timeout); abort.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [jobId, attempt]);
  return <>
    <div className="hosted-report-actions">
      {url ? <>
        <a className="hosted-pdf-link hosted-primary" href={url} target="_blank" rel="noopener noreferrer"><ExternalLink size={15} aria-hidden="true" />Open PDF</a>
        <a className="hosted-pdf-link" href={url} download="paper-overlap-report.pdf"><Download size={15} aria-hidden="true" />Download PDF</a>
      </> : <>
        <button disabled><ExternalLink size={15} aria-hidden="true" />Open PDF</button>
        <button disabled><Download size={15} aria-hidden="true" />Download PDF</button>
      </>}
    </div>
    {!url && <div className="hosted-pdf-transfer">
      {error ? <><p role="alert">{error}</p><button onClick={() => setAttempt(value => value + 1)}>Retry PDF transfer</button></>
        : <><p role="status">Preparing PDF access{bytes ? ` · ${Math.ceil(bytes / 1024)} KB received` : "…"}</p><button className="hosted-text-button" onClick={() => controller.current?.abort()}>Stop PDF transfer</button></>}
    </div>}
  </>;
}
