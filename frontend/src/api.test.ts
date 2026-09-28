import { describe, expect, it, vi, afterEach } from "vitest";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { api, hasReport, isActive, isTestFixture, json, percent, reportAvailable, safeUrl } from "./api";

afterEach(() => vi.unstubAllGlobals());

describe("screening state and safe links", () => {
  it("uses only explicit fixture tags, never a user's title", () => {
    expect(isTestFixture({ title: "Any test document", is_test_fixture: true })).toBe(true);
    expect(isTestFixture({ title: "Synthetic beginner paper mu123" })).toBe(false);
    expect(isTestFixture({ title: "Synthetic watershed manuscript mu123" })).toBe(false);
    expect(isTestFixture({ title: "Synthetic biology methods and clinical applications" })).toBe(false);
    expect(isTestFixture({ title: "My manuscript" })).toBe(false);
  });
  it("only polls queued and running jobs", () => {
    expect(["draft", "completed", "partial", "failed", "cancelled"].some(isActive)).toBe(false);
    expect(isActive("queued")).toBe(true);
    expect(isActive("running")).toBe(true);
    expect(hasReport("partial")).toBe(true);
    expect(hasReport("failed")).toBe(false);
    expect(reportAvailable({ status: "cancelled", report_available: true })).toBe(true);
    expect(reportAvailable({ status: "completed", report_available: false })).toBe(false);
    expect(reportAvailable({ status: "partial" })).toBe(true);
  });
  it("rejects executable and malformed source links", () => {
    expect(safeUrl("javascript:alert(1)")).toBeUndefined();
    expect(safeUrl("data:text/html,test")).toBeUndefined();
    expect(safeUrl("bad link")).toBeUndefined();
    expect(safeUrl("https://doi.org/10.1234/example")).toBe("https://doi.org/10.1234/example");
  });
  it("keeps backend percentage units and handles invalid numbers", () => {
    expect(percent(12.345)).toBe("12.3%");
    expect(percent(NaN)).toBe("—");
  });
});

describe("API requests", () => {
  it("uses the real API prefix and explicit JSON bodies", async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ id: "job" }) });
    vi.stubGlobal("fetch", fetch);
    expect(await api("/jobs", json("POST", { mode: "offline" }))).toEqual({ id: "job" });
    expect(fetch).toHaveBeenCalledWith("/api/jobs", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: '{"mode":"offline"}',
    });

  });
  it("shows server validation errors rather than an opaque failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 422, json: async () => ({ detail: [{ msg: "Target must be at least 100" }] }),
    }));
    await expect(api("/jobs/1")).rejects.toThrow("Target must be at least 100");
  });
});

describe("initial Clawpilot theme", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const script = html.match(/<script>([\s\S]*?)<\/script>/)?.[1] || "";
  function theme(search: string, prefersDark: boolean) {
    const setAttribute = vi.fn();
    runInNewContext(script, {
      URLSearchParams,
      window: { location: { search }, matchMedia: () => ({ matches: prefersDark }) },
      document: { documentElement: { setAttribute } },
    });
    return setAttribute.mock.calls[0]?.[1];
  }
  it("honors explicit light even when the operating system uses dark", () => {
    expect(theme("?clawpilotTheme=light", true)).toBe("light");
  });
  it("honors explicit dark even when the operating system uses light", () => {
    expect(theme("?clawpilotTheme=dark", false)).toBe("dark");
  });
  it("uses the operating system for missing or invalid overrides", () => {
    expect(theme("", true)).toBe("dark");
    expect(theme("?clawpilotTheme=invalid", false)).toBe("light");
  });
});
