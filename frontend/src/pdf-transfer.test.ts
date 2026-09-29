import { describe, expect, it } from "vitest";
import { readPdf } from "./pdf-transfer";

describe("validated PDF transfer", () => {
  it("returns a PDF blob and reports actual bytes without altering the payload", async () => {
    const bytes = new TextEncoder().encode("%PDF-1.7\nOriginal synthetic bytes\n%%EOF");
    const seen: number[] = [];
    const blob = await readPdf(new Response(bytes, { headers: { "Content-Type": "application/pdf" } }), n => seen.push(n));
    expect(blob.type).toBe("application/pdf");
    expect(new Uint8Array(await blob.arrayBuffer())).toEqual(bytes);
    expect(seen.at(-1)).toBe(bytes.length);
  });
  it("rejects HTML, an invalid signature and oversized content", async () => {
    await expect(readPdf(new Response("<html>error</html>", { headers: { "Content-Type": "text/html" } }), () => {})).rejects.toThrow("did not return a PDF");
    await expect(readPdf(new Response("not a PDF", { headers: { "Content-Type": "application/pdf" } }), () => {})).rejects.toThrow("not a valid PDF");
    await expect(readPdf(new Response("%PDF-", { headers: { "Content-Type": "application/pdf", "Content-Length": String(65 * 1024 * 1024) } }), () => {})).rejects.toThrow("transfer size");
  });
  it("surfaces a failed body stream rather than producing a success-shaped download", async () => {
    const stream = new ReadableStream<Uint8Array>({ start(controller) { controller.error(new Error("Synthetic body interruption")); } });
    await expect(readPdf(new Response(stream, { headers: { "Content-Type": "application/pdf" } }), () => {})).rejects.toThrow("body interruption");
  });
  it("does not confuse decoded bytes with a proxy's encoded content length", async () => {
    const response = new Response("%PDF-1.7\nDecoded synthetic content", { headers: { "Content-Type": "application/pdf", "Content-Length": "10" } });
    expect((await readPdf(response, () => {})).size).toBeGreaterThan(10);
  });
});
