const MAX_PDF_BYTES = 64 * 1024 * 1024;

export async function readPdf(response: Response, progress: (bytes: number) => void): Promise<Blob> {
  if (!response.ok) throw new Error(`PDF request failed (${response.status}). Your comparison has not been rerun.`);
  if (response.headers.get("content-type")?.split(";")[0].trim().toLowerCase() !== "application/pdf") {
    throw new Error("The service did not return a PDF. Retry the PDF transfer.");
  }
  const declared = Number(response.headers.get("content-length") || 0);
  if (declared > MAX_PDF_BYTES) throw new Error("The PDF exceeds the supported transfer size.");
  const chunks: ArrayBuffer[] = [];
  let size = 0;
  if (response.body) {
    const reader = response.body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > MAX_PDF_BYTES) {
          await reader.cancel();
          throw new Error("The PDF exceeds the supported transfer size.");
        }
        chunks.push(new Uint8Array(value).buffer);
        progress(size);
      }
    } finally { reader.releaseLock(); }
  } else {
    const buffer = await response.arrayBuffer();
    size = buffer.byteLength;
    if (size > MAX_PDF_BYTES) throw new Error("The PDF exceeds the supported transfer size.");
    chunks.push(buffer);
    progress(size);
  }
  const blob = new Blob(chunks, { type: "application/pdf" });
  if (new TextDecoder().decode(await blob.slice(0, 5).arrayBuffer()) !== "%PDF-") {
    throw new Error("The received file is not a valid PDF response. Retry the PDF transfer.");
  }
  return blob;
}
