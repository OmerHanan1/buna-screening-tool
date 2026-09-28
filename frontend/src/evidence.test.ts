import { describe, expect, it } from "vitest";
import { comparisonState, highlightedFragments, reuseCategory, sourceReason } from "./evidence";

describe("original evidence text and word-level highlights", () => {
  it("highlights only supplied shared words and preserves exact originals", () => {
    const text = "Shared words, different ending.";
    const fragments = highlightedFragments(text, [[0, 6], [7, 12]]);
    expect(fragments.filter((part) => part.highlighted).map((part) => part.text)).toEqual(["Shared", "words"]);
    expect(fragments.map((part) => part.text).join("")).toBe(text);
  });

  describe("authoritative source coverage", () => {
    it("does not count partial evidence as a fully compared article", () => {
      expect(comparisonState({ id: "p", status: "parsed", partial_comparison: true, compared: false })).toBe("partial");
      expect(comparisonState({ id: "p", status: "compared" }, [{ source_id: "p", status: "compared-with-limits" }])).toBe("partial");
    });
    it("prefers matcher coverage over stale paper status", () => {
      expect(comparisonState({ id: "p", status: "compared" }, [{ source_id: "p", status: "skipped-evidence-limit" }])).toBe("unexamined");
      expect(comparisonState({ id: "p", status: "parsed" }, [{ source_id: "p", status: "compared" }])).toBe("full");
    });
    it("explains normalized-identical exclusions and comparison limits", () => {
      expect(sourceReason({ id: "p", status: "excluded" }, [{ source_id: "p", status: "excluded-identical" }])).toContain("identical copy of your manuscript");
      expect(sourceReason({ id: "p", status: "parsed" }, [{ source_id: "p", status: "skipped-size-limit" }])).toContain("size limit");
      expect(sourceReason({ id: "p", status: "parsed" }, [{ source_id: "p", status: "compared-with-limits" }])).toContain("Only part");
    });
    it("preserves specific backend failure reasons", () => {
      expect(sourceReason({ id: "p", status: "parsed", error: "Stopped at 500 evidence spans." }, [{ source_id: "p", status: "compared-with-limits" }])).toBe("Stopped at 500 evidence spans.");
    });
  });
  it("merges overlapping ranges without duplicating evidence", () => {
    expect(highlightedFragments("abcdef", [[3, 6], [1, 4], [2, 3]])).toEqual([
      { text: "a", highlighted: false },
      { text: "bcdef", highlighted: true },
    ]);
  });
  it("uses Python-compatible Unicode offsets", () => {
    expect(highlightedFragments("📄 shared", [[2, 8]])).toEqual([
      { text: "📄 ", highlighted: false },
      { text: "shared", highlighted: true },
    ]);
  });
  it("does not manufacture word-level evidence when ranges are absent", () => {
    expect(highlightedFragments("Original text.")).toEqual([{ text: "Original text.", highlighted: false }]);
  });
  it("safely handles invalid and out-of-bounds ranges", () => {
    expect(highlightedFragments("abcdef", [[-5, 2], [4, 999], [8, 2], [NaN, 3]])).toEqual([
      { text: "ab", highlighted: true },
      { text: "cd", highlighted: false },
      { text: "ef", highlighted: true },
    ]);
  });
  it("labels same and near-identical reuse without a plagiarism verdict", () => {
    expect(reuseCategory("exact")).toBe("Exact reuse");
    expect(reuseCategory("near_identical")).toBe("Near-identical reuse");
  });
});
