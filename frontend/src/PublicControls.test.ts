import { describe, expect, it } from "vitest";
import { MAX_BATCH_BYTES, MAX_FILE_MIB, validateFile } from "./PublicControls";

describe("uniform upload capacity", () => {
  it.each([36, 40])("accepts a %i MiB PDF without reading its full contents", async mib => {
    const file = new File(["%PDF-"], "source.pdf", { type: "application/pdf" });
    Object.defineProperty(file, "size", { value: mib * 1024 * 1024 });
    expect(await validateFile(file)).toBe("");
  });
  it("rejects exactly one byte over and retains file type checks", async () => {
    const file = new File(["%PDF-"], "target.pdf");
    Object.defineProperty(file, "size", { value: 40 * 1024 * 1024 + 1 });
    expect(await validateFile(file)).toContain("40 MiB");
    expect(await validateFile(new File(["not a PDF"], "bad.pdf"))).toContain("PDF header");
    expect(await validateFile(new File(["data"], "bad.zip"))).toContain("plain-text");
    expect(await validateFile(new File([], "empty.txt"))).toContain("empty");
    expect(MAX_FILE_MIB).toBe(40);
    expect(MAX_BATCH_BYTES).toBe(128 * 1024 * 1024);
  });
});
