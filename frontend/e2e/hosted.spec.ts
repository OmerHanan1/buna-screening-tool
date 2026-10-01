import { test, expect } from "@playwright/test";
import fs from "node:fs/promises";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";

const site = process.env.HOSTED_SITE_URL || "https://kind-field-035b3910f.4.azurestaticapps.net/";
const backend = "https://paper-overlap-api.purpleflower-beedf5ce.eastus.azurecontainerapps.io";
const sitePath = new URL(site).pathname;
const staticConfig = JSON.parse(await fs.readFile("public/staticwebapp.config.json", "utf8"));

test("saved eligible denominator accounting agrees with the displayed percentage", async ({ page }) => {
  await page.route(site + "**", async route => route.fulfill(await staticResponse(route.request().url())));
  await page.route(backend + "/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/config")) return route.fulfill({ json: { mode: "email-gate" } });
    if (url.pathname.endsWith("/session")) return route.fulfill({ json: { token: "synthetic-policy-capability" } });
    if (url.pathname.endsWith("/library")) return route.fulfill({ json: { papers } });
    if (route.request().method() === "POST") return route.fulfill({ status: 202, json: { id: "policy-fixture", status: "running" } });
    if (url.pathname.endsWith(".pdf")) return route.fulfill({ contentType: "application/pdf", body: "%PDF-1.7\nSynthetic score-policy fixture" });
    return route.fulfill({ json: { id: "policy-fixture", status: "complete", checked: 44, total: 44,
      overlap_percent: 10, score_available: true, score_basis: "eligible-manuscript-word-units",
      score_policy_version: "eligible-manuscript-v1", algorithm_version: "2.5.4",
      word_accounting: { total_words: 200, scoped_words: 180, front_matter_words: 20, eligible_words: 140,
        score_denominator_words: 140, overlapping_words: 14, excluded_bibliography_words: 30,
        excluded_quotation_words: 10, other_excluded_manuscript_words: 0 } } });
  });
  await page.goto(site);
  await page.getByLabel("Email address").fill("synthetic@example.org");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("heading", { name: "44 papers selected" })).toBeVisible();
  await page.getByLabel("Your paper", { exact: true }).setInputFiles({ name: "synthetic.txt", mimeType: "text/plain", buffer: Buffer.from("Original synthetic manuscript.") });
  await page.getByRole("checkbox", { name: /authorized to upload/ }).check();
  await page.getByRole("button", { name: "Compare papers", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Comparison report", exact: true })).toBeVisible();
  await expect(page.getByText("10%", { exact: true })).toBeVisible();
  await page.getByText("Report details and evidence", { exact: true }).click();
  await expect(page.getByText(/14 unique matching words \/ 140 eligible manuscript words after exclusions/)).toBeVisible();
  await expect(page.getByText(/200 total; 20 front matter excluded; 180 in scope; 30 bibliography, 10 quoted/)).toBeVisible();
});

test("global removal confirms specific bundled and shared papers with Cancel focused", async ({ page }) => {
  let current = papers.slice(0, 2).map((paper, index) => ({ ...paper, library_version: "0", storage_kind: index ? "shared" : "bundled" }));
  const removed: string[] = [];
  await page.route(site + "**", async route => route.fulfill(await staticResponse(route.request().url())));
  await page.route(backend + "/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/config")) return route.fulfill({ json: { mode: "email-gate" } });
    if (url.pathname.endsWith("/session")) return route.fulfill({ json: { token: "synthetic-removal-capability" } });
    if (url.pathname.endsWith("/library")) return route.fulfill({ json: { papers: current, library_removal_available: true, shared_saving_available: true, immediate_shared_saving: true } });
    if (route.request().method() === "DELETE" && url.pathname.includes("/library/")) {
      const id = url.pathname.split("/").at(-1)!;
      expect(route.request().postDataJSON()).toEqual({ confirm_sha256: id, expected_version: "0", affects_everyone: true });
      expect(route.request().headers().authorization).toContain("synthetic-removal-capability");
      removed.push(id);
      current = current.filter(paper => paper.sha256 !== id);
      return route.fulfill({ json: { removed: true, scope: "all-users" } });
    }
    return route.fulfill({ status: 404 });
  });
  await page.goto(site);
  await page.getByLabel("Email address").fill("synthetic@example.org");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("heading", { name: "2 papers selected" })).toBeVisible();
  await page.getByRole("button", { name: "Review papers", exact: true }).click();
  await page.locator(".hosted-source-item input").first().uncheck();
  expect(removed).toHaveLength(0);
  const firstTitle = current[0].title;
  await page.getByRole("button", { name: `Remove from library: ${firstTitle}`, exact: true }).click();
  const confirm = page.getByRole("alertdialog", { name: "Remove for everyone?" });
  await expect(confirm).toContainText(firstTitle);
  await expect(confirm).toContainText("all app users");
  await expect(confirm).toContainText("remains packaged privately");
  await expect(confirm.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(confirm).not.toBeVisible();
  await expect(page.getByRole("dialog", { name: "Comparison papers" })).toBeVisible();
  expect(removed).toHaveLength(0);
  await page.getByRole("button", { name: `Remove from library: ${firstTitle}`, exact: true }).click();
  await confirm.getByRole("button", { name: "Remove from library", exact: true }).click();
  await expect(page.locator(".hosted-source-item")).toHaveCount(1);
  await page.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByText("From the 1-paper comparison library", { exact: true })).toBeVisible();
  await page.reload();
  await page.getByLabel("Email address").fill("another-synthetic@example.org");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("heading", { name: "1 papers selected" })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "Review papers", exact: true }).click();
  await page.getByRole("button", { name: `Remove from library: ${current[0].title}`, exact: true }).click();
  await expect(confirm).toContainText("after a retention delay");
  await expect(confirm.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await confirm.getByRole("button", { name: "Remove from library", exact: true }).click();
  await expect(page.locator(".hosted-source-item")).toHaveCount(0);
  await page.getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByRole("heading", { name: "0 papers selected" })).toBeVisible();
  expect(removed).toHaveLength(2);
});

async function staticResponse(url: string) {
  const pathname = new URL(url).pathname;
  expect(pathname.startsWith(sitePath)).toBe(true);
  const name = pathname.slice(sitePath.length) || "index.html";
  const filename = path.resolve("../public-dist", name);
  expect(filename.startsWith(path.resolve("../public-dist") + path.sep)).toBe(true);
  return {
    body: await fs.readFile(filename),
    contentType: name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "text/html",
    headers: staticConfig.globalHeaders,
  };
}

test("opt-in actual hosted PDF workflow", async ({ page }) => {
  test.skip(!process.env.HOSTED_TEST_EMAIL, "Requires explicit authorized live-test email.");
  test.setTimeout(1_020_000);
  page.setDefaultTimeout(90_000);
  await page.goto(site, { waitUntil: "domcontentloaded" });
  await page.getByLabel("Email address").fill("wrong@example.invalid");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Access is not available");
  await page.getByLabel("Email address").fill(process.env.HOSTED_TEST_EMAIL!);
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("heading", { name: "44 papers selected", exact: true })).toBeVisible({ timeout: 90_000 });
  await page.screenshot({ path: test.info().outputPath("live-setup-desktop.png"), fullPage: true });
  await page.getByRole("button", { name: "Review papers", exact: true }).click();
  await expect(page.locator(".hosted-source-item input")).toHaveCount(44);
  await page.locator(".hosted-source-item input").first().uncheck();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("heading", { name: "43 papers selected", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Review papers", exact: true }).click();
  await expect(page.locator(".hosted-source-item input").first()).not.toBeChecked();
  await page.locator(".hosted-source-item input").first().check();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("heading", { name: "44 papers selected", exact: true })).toBeVisible();
  const sentence = "The ceramic sensor records a stable sequence of local measurements during every carefully controlled laboratory cycle.";
  const pdf = (title: string) => {
    const result = spawnSync("../.venv/bin/python", ["-c",
      "import pymupdf as f,sys;d=f.open();p=d.new_page();p.insert_text((50,60),sys.argv[1]);p.insert_text((50,90),'Abstract');p.insert_text((50,120),sys.argv[2],fontsize=8);p.insert_text((50,150),'Original ending unique to '+sys.argv[1]);sys.stdout.buffer.write(d.tobytes())",
      title, sentence]);
    expect(result.status).toBe(0); return result.stdout;
  };
  const realistic = process.env.HOSTED_REALISTIC === "true";
  let target = pdf("Synthetic manuscript");
  if (realistic) {
    const result = spawnSync("../.venv/bin/python", ["-c",
      "import sys,tempfile;from pathlib import Path;sys.path.insert(0,'../deploy');from benchmark_hosted import manuscript\nwith tempfile.TemporaryDirectory() as d:\n p=Path(d)/'synthetic.pdf';manuscript(p);sys.stdout.buffer.write(p.read_bytes())"]);
    expect(result.status).toBe(0);
    target = result.stdout;
  }
  await page.getByLabel("Your paper", { exact: true }).setInputFiles({ name: "Synthetic manuscript.pdf", mimeType: "application/pdf", buffer: target });
  await expect(page.getByText("Ready to upload", { exact: true })).toBeVisible();
  await page.getByLabel("Additional comparison papers").setInputFiles({ name: "Synthetic source.pdf", mimeType: "application/pdf", buffer: pdf("Synthetic source") });
  await expect(page.getByRole("button", { name: "Remove comparison Synthetic source.pdf" })).toBeVisible();
  await page.getByRole("checkbox", { name: /authorized to upload/ }).check();
  await page.screenshot({ path: test.info().outputPath("live-files-selected.png"), fullPage: true });
  const creation = page.waitForResponse(response =>
    response.url() === backend + "/api/public/jobs" && response.request().method() === "POST");
  await page.getByRole("button", { name: "Compare papers", exact: true }).click();
  const created = await creation;
  expect(created.status()).toBe(202);
  const jobId = (await created.json()).id;
  const secondSession = await page.request.post(backend + "/api/public/session", {
    headers: { Origin: new URL(site).origin }, data: { email: process.env.HOSTED_TEST_EMAIL },
  });
  expect(secondSession.status()).toBe(200);
  const secondHeaders = {
    Origin: new URL(site).origin, Authorization: `Bearer ${(await secondSession.json()).token}`,
  };
  for (const suffix of ["", "/report.pdf", "/report.json"]) {
    expect((await page.request.get(`${backend}/api/public/jobs/${jobId}${suffix}`, { headers: secondHeaders })).status()).toBe(404);
  }
  expect((await page.request.delete(`${backend}/api/public/jobs/${jobId}`, { headers: secondHeaders })).status()).toBe(404);
  for (const route of ["/api/public/sources", "/api/admin", "/corpus/manifest.json"]) {
    expect((await page.request.get(backend + route, { headers: secondHeaders })).status()).toBe(404);
  }
  expect((await page.request.get(backend + "/api/public/library", {
    headers: { Origin: new URL(site).origin },
  })).status()).toBe(401);
  for (const origin of ["https://omerhanan1.github.io", "https://untrusted.example.invalid"]) {
    expect((await page.request.get(backend + "/api/auth/config", { headers: { Origin: origin } })).status()).toBe(403);
  }
  await expect(page.getByRole("heading", { name: "Comparing your manuscript", exact: true })).toBeVisible({ timeout: 90_000 });
  await page.screenshot({ path: test.info().outputPath("live-progress.png"), fullPage: true });
  await expect(page.getByRole("heading", { name: "Comparison report", exact: true })).toBeVisible({ timeout: 840_000 });
  await expect(page.getByText("45 / 45", { exact: true })).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("live-result-desktop.png"), fullPage: true });
  const popupWait = page.waitForEvent("popup");
  await page.getByRole("link", { name: "Open PDF", exact: true }).click();
  const popup = await popupWait;
  await expect.poll(() => popup.url(), { timeout: 90_000 }).toMatch(/^blob:/);
  await popup.close();
  const downloadWait = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download PDF", exact: true }).click();
  const download = await downloadWait;
  expect(download.suggestedFilename()).toBe("paper-overlap-report.pdf");
  const chunks: Buffer[] = [];
  for await (const chunk of (await download.createReadStream())!) chunks.push(Buffer.from(chunk));
  const bytes = Buffer.concat(chunks);
  expect(bytes.subarray(0, 5).toString()).toBe("%PDF-");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: test.info().outputPath("live-result-mobile.png"), fullPage: true });
  await page.evaluate(() => document.documentElement.setAttribute("data-theme", "dark"));
  await page.screenshot({ path: test.info().outputPath("live-result-mobile-dark.png"), fullPage: true });
  page.on("dialog", dialog => dialog.accept());
  await page.getByRole("button", { name: "Delete report", exact: true }).click();
  await expect(page.getByRole("heading", { name: "New comparison", exact: true })).toBeVisible();
  await fs.writeFile(test.info().outputPath("live-proof.json"), JSON.stringify({
    url: site, default_sources: 44, optout_retained: 43, restored_sources: 44, synthetic_manual_sources: 1,
    realistic_manuscript_pages: realistic ? 45 : 1,
    fully_checked: 45, pdf_bytes: bytes.length, open_pdf_blob: true, deleted: true,
    independent_same_email_visitor_denied: true, source_admin_routes_denied: true,
    old_and_unknown_origins_denied: true, unauthenticated_library_denied: true,
  }, null, 2));
});

test("cold-start readiness and connection retry stay explicit", async ({ page }) => {
  let ready = false;
  let finishConnection: () => void = () => {};
  const connection = new Promise<void>(resolve => { finishConnection = resolve; });
  await page.route(site + "**", async route => route.fulfill(await staticResponse(route.request().url())));
  await page.route(backend + "/api/auth/config", async route => {
    await connection;
    await route.fulfill(ready ? { json: { mode: "email-gate" } } : { status: 503, json: { detail: "Starting." } });
  });
  await page.goto(site, { waitUntil: "domcontentloaded" });
  await expect(page.getByText("Made for Orchuk", { exact: true })).toBeVisible();
  await expect(page.getByText("Text similarity for research", { exact: true })).toHaveCount(0);
  await expect(page.locator("footer")).toHaveText("For research review.Source code · AGPL");
  await page.getByLabel("Email address").fill("allowed@example.org");
  await expect(page.getByRole("button", { name: "Continue", exact: true })).toBeDisabled();
  await expect(page.getByRole("status")).toContainText("First access may take a moment");
  finishConnection();
  await expect(page.getByRole("alert")).toContainText("service could not be reached");
  await expect(page.getByRole("button", { name: "Continue", exact: true })).toBeDisabled();
  ready = true;
  await page.getByRole("button", { name: "Retry connection", exact: true }).click();
  await page.getByLabel("Email address").fill("allowed@example.org");
  await expect(page.getByRole("button", { name: "Continue", exact: true })).toBeEnabled();
});
const papers = Array.from({ length: 44 }, (_, i) => ({
  sha256: i.toString(16).padStart(64, "0"),
  title: [
    "Emotion regulation and attention in everyday decision making",
    "How psychological distance shapes memory and social judgment",
    "Cognitive flexibility across the lifespan: a longitudinal study",
    "The role of context in evaluating emotional experiences",
  ][i % 4] + ` — study ${i + 1}`,
  version: i % 3 ? "Published article · Original complete PDF" : "Author manuscript · Institutional repository",
  attribution: "Synthetic Author. Original test fixture; not an actual scientific paper.",
  license: "Synthetic test fixture", license_url: "https://example.org/license",
}));

test("PDF transfer timeout is retryable without rerunning or losing the comparison", async ({ page }) => {
  let holdPdf = true, pdfRequests = 0, comparisons = 0;
  await page.clock.install();
  await page.route(site + "**", async route => {
    const name = new URL(route.request().url()).pathname.replace(new URL(site).pathname, "") || "index.html";
    const filename = path.resolve("../public-dist", name);
    await route.fulfill({ body: await fs.readFile(filename), contentType: name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "text/html" });
  });
  await page.route(backend + "/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/config")) return route.fulfill({ json: { mode: "email-gate" } });
    if (url.pathname.endsWith("/session")) return route.fulfill({ json: { token: "synthetic-transfer-capability" } });
    if (url.pathname.endsWith("/library")) return route.fulfill({ json: { papers } });
    if (route.request().method() === "POST") {
      comparisons++; return route.fulfill({ status: 202, json: { id: "transfer-fixture", status: "running" } });
    }
    if (url.pathname.endsWith(".pdf")) {
      pdfRequests++;
      if (holdPdf) return;
      return route.fulfill({ contentType: "application/pdf", body: "%PDF-1.7\nSynthetic transfer fixture" });
    }
    return route.fulfill({ json: { id: "transfer-fixture", status: "complete", checked: 44, total: 44, overlap_percent: 1.2, score_available: true } });
  });
  await page.goto(site);
  await page.getByLabel("Email address").fill("fixture@example.org");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("heading", { name: "44 papers selected" })).toBeVisible();
  await page.getByLabel("Your paper", { exact: true }).setInputFiles({ name: "synthetic.txt", mimeType: "text/plain", buffer: Buffer.from("Original synthetic target text.") });
  await page.getByRole("checkbox", { name: /authorized to upload/ }).check();
  await page.getByRole("button", { name: "Compare papers", exact: true }).click();
  await page.clock.fastForward(2000);
  await expect(page.getByRole("heading", { name: "Comparison report", exact: true })).toBeVisible();
  await expect(page.getByText("Preparing PDF access…", { exact: true })).toBeVisible();
  await page.clock.fastForward(61000);
  await expect(page.getByRole("alert")).toContainText("PDF transfer timed out");
  await expect(page.getByRole("heading", { name: "Comparison report", exact: true })).toBeVisible();
  holdPdf = false;
  await page.getByRole("button", { name: "Retry PDF transfer", exact: true }).click();
  await expect(page.getByRole("link", { name: "Open PDF", exact: true })).toHaveAttribute("href", /^blob:/);
  const downloadWait = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download PDF", exact: true }).click();
  expect((await downloadWait).suggestedFilename()).toBe("paper-overlap-report.pdf");
  expect(comparisons).toBe(1);
  expect(pdfRequests).toBe(2);
});

for (const variant of [
  { name: "desktop-light", width: 1440, height: 1000, theme: "light" },
  { name: "laptop-light", width: 1100, height: 800, theme: "light" },
  { name: "mobile-light", width: 390, height: 844, theme: "light" },
  { name: "desktop-dark", width: 1440, height: 1000, theme: "dark" },
  { name: "mobile-dark", width: 390, height: 844, theme: "dark" },
]) {
  test(`hosted workspace ${variant.name}`, async ({ page }) => {
    await page.setViewportSize(variant);
    let complete = false, submitted = false;
    await page.route(site + "**", async route => {
      await route.fulfill(await staticResponse(route.request().url()));
    });
    await page.route(backend + "/**", async route => {
      const url = new URL(route.request().url()), method = route.request().method();
      const respond = (json: unknown, status = 200) => route.fulfill({ json, status });
      if (url.pathname === "/api/auth/config") return respond({ mode: "email-gate" });
      if (url.pathname.endsWith("/session")) {
        return route.request().postDataJSON().email.trim().toLowerCase() === "allowed@example.org"
          ? respond({ token: "synthetic-capability-only" }) : respond({ detail: "Access is not available for this entry." }, 403);
      }

      expect(route.request().headers().authorization).toBe("Bearer synthetic-capability-only");
      if (url.pathname.endsWith("/library")) return respond({ papers });
      if (url.pathname.endsWith("/source-uploads")) return respond({ id: "synthetic-upload", state: "ready", digest: "f".repeat(64) }, 201);
      if (method === "POST") { submitted = true; return respond({ id: "synthetic-job", status: "running" }, 202); }
      if (method === "DELETE") return respond({ status: "deleted" });
      if (url.pathname.endsWith(".pdf")) return route.fulfill({ contentType: "application/pdf", body: "%PDF-1.7\nSynthetic download signature fixture" });
      return respond(complete ? { id: "synthetic-job", status: "complete", checked: 44, total: 44, overlap_percent: 8.42, score_available: true, warnings: ["Analysis starts at the Abstract heading on page 2; 18 preceding front-matter words were excluded."] } : { id: "synthetic-job", status: "running" });
    });
    await page.goto(site + "?clawpilotTheme=" + variant.theme);
    await expect(page.getByRole("button", { name: "Continue", exact: true })).toBeDisabled();
    await page.screenshot({ path: test.info().outputPath("01-email.png"), fullPage: true });
    await page.getByLabel("Email address").fill("wrong@example.org");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("Access is not available");
    await page.screenshot({ path: test.info().outputPath("02-email-error.png"), fullPage: true });
    await page.getByLabel("Email address").fill("allowed@example.org");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(page.getByRole("heading", { name: "44 papers selected" })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath("03-setup.png"), fullPage: true });
    await page.getByRole("button", { name: "Review papers", exact: true }).click();
    await expect(page.getByRole("dialog")).toBeVisible();
    await page.getByLabel("Search comparison papers").fill("memory");
    await expect(page.locator(".hosted-source-item")).toHaveCount(11);
    await page.getByLabel("Search comparison papers").fill("");
    await page.locator(".hosted-source-item input").first().uncheck();
    await expect(page.getByText("43 of 44 selected", { exact: true })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath("04-library.png"), fullPage: true });
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Review papers", exact: true })).toBeFocused();
    await expect(page.getByRole("heading", { name: "43 papers selected" })).toBeVisible();
    await page.getByLabel("Your paper", { exact: true }).setInputFiles({ name: "bad.pdf", mimeType: "application/pdf", buffer: Buffer.from("not a PDF") });
    await expect(page.getByRole("alert")).toContainText("valid PDF header");
    await page.getByLabel("Your paper", { exact: true }).setInputFiles({ name: "Synthetic manuscript — attention and memory.txt", mimeType: "text/plain", buffer: Buffer.from("Abstract\nAn original synthetic manuscript for interface testing.") });
    await expect(page.getByText("Ready to upload", { exact: true })).toBeVisible();
    await page.getByLabel("Additional comparison papers").setInputFiles({ name: "Additional source.txt", mimeType: "text/plain", buffer: Buffer.from("Synthetic source material.") });
    await expect(page.getByRole("button", { name: "Remove comparison Additional source.txt" })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath("05-files-selected.png"), fullPage: true });
    await expect(page.getByRole("button", { name: "Compare papers", exact: true })).toBeDisabled();
    await page.getByRole("checkbox", { name: /authorized to upload/ }).check();
    await page.getByRole("button", { name: "Compare papers", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Comparing your manuscript" })).toBeVisible();
    expect(submitted).toBe(true);
    await page.screenshot({ path: test.info().outputPath("06-progress.png"), fullPage: true });
    complete = true;
    await expect(page.getByRole("heading", { name: "Comparison report", exact: true })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath("07-result.png"), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    const download = page.waitForEvent("download");
    await page.getByRole("link", { name: "Download PDF", exact: true }).click();
    expect((await download).suggestedFilename()).toBe("paper-overlap-report.pdf");
    expect(await page.evaluate(() => Object.keys(sessionStorage).some(key => key.includes("paper-overlap-public")))).toBe(false);
    page.on("dialog", dialog => dialog.accept());
    await page.getByRole("button", { name: "Delete report", exact: true }).click();
    await expect(page.getByRole("heading", { name: "New comparison", exact: true })).toBeVisible();
  });
}

test("expired sessions and partial/no-score results stay explicit", async ({ page }) => {
  let expired = false, complete = false;
  await page.route(site + "**", async route => {
    await route.fulfill(await staticResponse(route.request().url()));
  });
  await page.route(backend + "/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/config")) return route.fulfill({ json: { mode: "email-gate" } });
    if (url.pathname.endsWith("/session")) return route.fulfill({ json: { token: "synthetic-only" } });
    if (url.pathname.endsWith("/library")) return route.fulfill({ json: { papers } });
    if (route.request().method() === "POST") return route.fulfill({ json: { id: "test", status: "running" }, status: 202 });
    if (expired) return route.fulfill({ status: 401, json: { detail: "Expired." } });
    if (url.pathname.endsWith(".pdf")) return route.fulfill({ contentType: "application/pdf", body: "%PDF-1.7\nSynthetic fixture" });
    return route.fulfill({ json: complete ? {
      id: "test", status: "complete", checked: 0, total: 44, partial: true, score_available: false,
      warnings: ["Abstract heading not detected; the whole manuscript was analyzed (front matter was not excluded)."],
    } : { id: "test", status: "running" } });
  });
  await page.goto(site);
  await page.getByLabel("Email address").fill("allowed@example.org");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByRole("heading", { name: "44 papers selected" }).waitFor();
  await page.getByLabel("Your paper", { exact: true }).setInputFiles({ name: "fixture.txt", mimeType: "text/plain", buffer: Buffer.from("Synthetic text.") });
  await page.getByRole("checkbox", { name: /authorized to upload/ }).check();
  await page.getByRole("button", { name: "Compare papers", exact: true }).click();
  complete = true;
  await expect(page.getByRole("heading", { name: "Partial comparison report", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Comparison report", exact: true })).toHaveCount(0);
  await expect(page.getByText("Not assessed", { exact: true })).toBeVisible();
  await expect(page.getByText(/Comparison incomplete. No score is available/)).toBeVisible();
  await expect(page.getByText(/Abstract heading not detected/)).toBeVisible();
  expired = true;
  await page.getByText("Report details and evidence", { exact: true }).click();
  await page.getByRole("button", { name: "Download evidence JSON", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("workspace expired");
  await expect(page.getByLabel("Email address")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Comparison report", exact: true })).toHaveCount(0);
});

for (const model of ["classified-v1.1", "improvedEng"]) {
test(`${model} requires explicit selection and resets for a new comparison`, async ({ page }) => {
  let postedModel = "";
  await page.route(site + "**", async route => {
    const name = new URL(route.request().url()).pathname.replace(new URL(site).pathname, "") || "index.html";
    await route.fulfill({ body: await fs.readFile(path.resolve("../public-dist", name)), contentType: name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "text/html" });
  });
  await page.route(backend + "/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/config")) return route.fulfill({ json: { mode: "email-gate" } });
    if (url.pathname.endsWith("/session")) return route.fulfill({ json: { token: "synthetic-model-capability" } });
    if (url.pathname.endsWith("/library")) return route.fulfill({ json: { papers } });
    if (route.request().method() === "POST") {
      postedModel = route.request().postData()?.includes(model) ? model : "validated-lexical";
      return route.fulfill({ status: 202, json: { id: "model-fixture", status: "running" } });
    }
    if (url.pathname.endsWith(".pdf")) return route.fulfill({ contentType: "application/pdf", body: "%PDF-1.7\nSynthetic model fixture" });
    if (url.pathname.endsWith(".csv")) return route.fulfill({ contentType: "text/csv", body: "source,anchor_length\n1,4\n" });
    return route.fulfill({ json: {
      id: "model-fixture", status: "complete", comparison_model: postedModel,
      algorithm_version: postedModel === "improvedEng" ? "improvedEng-v3-precision" : postedModel,
      eligibility_profile: postedModel === "improvedEng" ? "improvedEng-layout-longquotes-v1" : undefined,
      checked: 44, total: 44, overlap_percent: 12.5, score_available: true,
      classification_counts: { exact_words: 9, similar_only_words: 6, unmatched_words: 10, not_fully_checked_words: 0 },
    } });
  });
  await page.goto(site);
  await page.getByLabel("Email address").fill("fixture@example.org");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("heading", { name: "44 papers selected" })).toBeVisible();
  await page.getByText("Advanced", { exact: true }).click();
  await expect(page.getByLabel("Comparison model")).toHaveValue("validated-lexical");
  await page.getByLabel("Comparison model").selectOption(model);
  await expect(page.getByText(model === "improvedEng" ? /Experimental: Similar requires nine eligible non-citation matches, four distinct content words/ : /Experimental: separates exact wording/)).toBeVisible();
  await page.getByLabel("Your paper", { exact: true }).setInputFiles({ name: "synthetic.txt", mimeType: "text/plain", buffer: Buffer.from("Original synthetic target text.") });
  await page.getByRole("checkbox", { name: /authorized to upload/ }).check();
  await page.getByRole("button", { name: "Compare papers", exact: true }).click();
  await expect(page.getByText(model === "improvedEng"
    ? "improvedEng experimental ordered lexical report. Scores may differ from the standard model; no verified Crossref equivalence."
    : "Experimental exact + similar wording report. Scores may differ from the standard model.", { exact: true })).toBeVisible();
  expect(postedModel).toBe(model);
  await page.getByText("Report details and evidence", { exact: true }).click();
  await expect(page.getByText(/9 exact words · 6 similar-only words/)).toBeVisible();
  if (model === "improvedEng") {
    await expect(page.getByText(/Saved eligibility profile: improvedEng-layout-longquotes-v1/)).toBeVisible();
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download Similar diagnostics CSV" }).click();
    expect((await download).suggestedFilename()).toBe("paper-overlap-report.csv");
  }
  page.on("dialog", dialog => dialog.accept());
  await page.getByRole("button", { name: "New comparison", exact: true }).click();
  await page.getByText("Advanced", { exact: true }).click();
  await expect(page.getByLabel("Comparison model")).toHaveValue("validated-lexical");
});
}

test("checkbox saves without a manuscript or comparison and refreshed selections do not double count", async ({ page }) => {
  let saved = false;
  let submitted = "";
  let saveSubmitted = "", comparisons = 0;
  let releaseSave: () => void = () => {};
  const pending = new Promise<void>(resolve => { releaseSave = resolve; });
  const addition = { ...papers[0], sha256: "f".repeat(64), title: "New shared synthetic paper" };
  await page.route(site + "**", async route => {
    const name = new URL(route.request().url()).pathname.replace(new URL(site).pathname, "") || "index.html";
    await route.fulfill({ body: await fs.readFile(path.resolve("../public-dist", name)), contentType: name.endsWith(".js") ? "text/javascript" : name.endsWith(".css") ? "text/css" : "text/html" });
  });
  await page.route(backend + "/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/config")) return route.fulfill({ json: { mode: "email-gate" } });
    if (url.pathname.endsWith("/session")) return route.fulfill({ json: { token: "synthetic-shared-capability" } });
    if (url.pathname.endsWith("/library")) return route.fulfill({ json: { papers: saved ? [...papers, addition] : papers, shared_saving_available: true, immediate_shared_saving: true } });
    if (url.pathname.endsWith("/source-uploads")) return route.fulfill({ status: 201, json: { id: "private-upload", state: "ready", digest: addition.sha256 } });
    if (url.pathname.endsWith("/source-saves")) {
      saveSubmitted = route.request().postData() || "";
      return route.fulfill({ status: 202, json: { id: "source-receipt", state: "queued", digest: addition.sha256 } });
    }
    if (url.pathname.endsWith("/source-saves/source-receipt")) {
      await pending; saved = true;
      return route.fulfill({ json: { id: "source-receipt", state: "saved", digest: addition.sha256 } });
    }
    if (route.request().method() === "POST") {
      submitted = route.request().postData() || "";
      comparisons++;
      return route.fulfill({ status: 202, json: { id: "shared-fixture", status: "running", source_count: 44 } });
    }
    if (url.pathname.endsWith(".pdf")) return route.fulfill({ contentType: "application/pdf", body: "%PDF-1.7\nSynthetic shared fixture" });
    return route.fulfill({ json: { id: "shared-fixture", status: "complete", checked: 44, total: 44, overlap_percent: 12, score_available: true,
      library_saves: [] } });
  });

  await page.goto(site);
  await page.getByLabel("Email address").fill("fixture@example.org");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("heading", { name: "44 papers selected" })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: /Keep in library/ })).toHaveCount(0);
  await page.getByLabel("Additional comparison papers").setInputFiles({ name: "comparison.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7\nSynthetic source header fixture") });
  const keep = page.getByRole("checkbox", { name: /Keep in library for future comparisons/ });
  await expect(keep).not.toBeChecked();
  await page.getByRole("button", { name: "Review papers", exact: true }).click();
  await page.locator(".hosted-source-item input").first().uncheck();
  await page.keyboard.press("Escape");
  await keep.check();
  await expect(page.getByRole("status", { name: "Save status: comparison.pdf" })).toContainText("Saving queued");
  expect(comparisons).toBe(0);
  await expect.poll(() => saveSubmitted).toContain('name="share_authorized"');
  expect(saveSubmitted).not.toContain('name="target"');
  await page.getByLabel("Your paper", { exact: true }).setInputFiles({ name: "manuscript.pdf", mimeType: "application/pdf", buffer: Buffer.from("%PDF-1.7\nSynthetic header fixture") });
  await page.getByRole("checkbox", { name: /authorized to upload/ }).check();
  await expect(page.getByRole("button", { name: "Compare papers", exact: true })).toBeDisabled();
  releaseSave();
  await expect(page.getByRole("status", { name: "Save status: comparison.pdf" })).toContainText("Saved to shared library.");
  await expect(page.getByText("From the 45-paper comparison library", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "44 papers selected" })).toBeVisible();
  await expect(page.getByText("Ready to compare against 44 papers.", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Compare papers", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Comparison report", exact: true })).toBeVisible();
  expect(submitted).not.toContain('name="save_sources"');
  expect(submitted).not.toContain('name="sources"');
  expect(submitted).toContain(addition.sha256);
  expect(comparisons).toBe(1);
  page.on("dialog", dialog => dialog.accept());
  await page.getByRole("button", { name: "New comparison", exact: true }).click();
  await expect(page.getByText("From the 45-paper comparison library", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "44 papers selected" })).toBeVisible();
  await page.getByRole("button", { name: "Review papers", exact: true }).click();
  await expect(page.locator(".hosted-source-item input").first()).not.toBeChecked();
  await expect(page.locator(".hosted-source-item input").last()).toBeChecked();
});

for (const method of ["chooser", "drop"] as const) test(`bulk ${method}: 50 original PDFs, private queue and one 94-source comparison`, async ({ page }) => {
    test.setTimeout(180_000);
    const generated = spawnSync("../.venv/bin/python", ["-c",
      "import json,base64,pymupdf;out=[]\nfor i in range(50):\n d=pymupdf.open();p=d.new_page();p.insert_text((50,60),f'Original synthetic research paper {i}');p.insert_text((50,90),'Introduction: Independent experiments and complete source conclusions.');out.append(base64.b64encode(d.tobytes()).decode());d.close()\nprint(json.dumps(out))"]);
    expect(generated.status).toBe(0);
    const files = (JSON.parse(generated.stdout.toString()) as string[]).map((value, index) => ({
      name: `Original ${index}.pdf`, mimeType: "application/pdf", buffer: Buffer.from(value, "base64"),
    }));
    let uploads = 0, compares = 0, saves = 0, posted = "";
    await page.clock.install();
    await page.route(site + "**", async route => route.fulfill(await staticResponse(route.request().url())));
    await page.route(backend + "/**", async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith("/config")) return route.fulfill({ json: { mode: "email-gate" } });
      if (url.pathname.endsWith("/session")) return route.fulfill({ json: { token: "synthetic-bulk-capability" } });
      if (url.pathname.endsWith("/library")) return route.fulfill({ json: { papers, shared_saving_available: true, immediate_shared_saving: true } });
      if (url.pathname.endsWith("/source-uploads")) {
        const index = uploads++;
        return route.fulfill({ status: 201, json: { id: `private-${index}`, state: "ready",
          digest: createHash("sha256").update(files[index].buffer).digest("hex") } });
      }
      if (url.pathname.endsWith("/source-saves")) { saves++; return route.fulfill({ status: 500 }); }
      if (url.pathname.endsWith("/jobs")) {
        compares++; posted = route.request().postData() || "";
        return route.fulfill({ status: 202, json: { id: "bulk-comparison", status: "running", source_count: 94 } });
      }
      return route.fulfill({ json: { id: "bulk-comparison", status: "complete", checked: 94, total: 94, overlap_percent: 0 } });
    });
    await page.goto(site);
    await page.getByLabel("Email address").fill("fixture@example.org");
    await page.getByRole("button", { name: "Continue", exact: true }).click();
    await expect(page.getByRole("heading", { name: "44 papers selected" })).toBeVisible();
    if (method === "chooser") await page.getByLabel("Additional comparison papers").setInputFiles(files);
    else {
      const transfer = await page.evaluateHandle(values => {
        const data = new DataTransfer();
        values.forEach((value, index) => data.items.add(new File([Uint8Array.from(atob(value), character => character.charCodeAt(0))], `Original ${index}.pdf`, { type: "application/pdf" })));
        return data;
      }, files.map(file => file.buffer.toString("base64")));
      await page.getByRole("region", { name: "Comparison upload queue" }).dispatchEvent("drop", { dataTransfer: transfer });
    }
    await expect(page.getByRole("button", { name: /^Remove comparison/ })).toHaveCount(50);
    await page.getByLabel("Your paper", { exact: true }).setInputFiles({ name: "target.txt", mimeType: "text/plain", buffer: Buffer.from("Original manuscript only.") });
    await page.getByRole("checkbox", { name: /authorized to upload/ }).check();
    await expect(page.getByRole("button", { name: "Compare papers", exact: true })).toBeDisabled();
    for (let index = 0; index < 50; index++) {
      await page.clock.runFor(2200);
      await expect(page.getByRole("status", { name: `Upload status: Original ${index}.pdf`, exact: true })).toContainText("Ready for comparison");
    }
    await expect(page.getByText("Ready to compare against 94 papers.", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Compare papers", exact: true }).click();
    await page.clock.runFor(1500);
    await expect(page.getByText("94 / 94", { exact: true })).toBeVisible();
    expect(uploads).toBe(50); expect(compares).toBe(1); expect(saves).toBe(0);
    expect(posted).toContain('name="uploaded_sources"');
    expect(posted).not.toContain('name="sources"');
    for (let index = 0; index < 50; index++) expect(posted).toContain(`"private-${index}"`);
});

test("bulk keep queues 50 saves without a manuscript and a fresh visitor selects durable sources", async ({ page }) => {
  await page.clock.install();
  const additions: typeof papers = [];
  let privateUploads = 0, saves = 0, comparisons = 0, submitted = "";
  const files = Array.from({ length: 50 }, (_, index) => ({
    name: `Save ${index}.pdf`, mimeType: "application/pdf", buffer: Buffer.from(`%PDF-1.7\nOriginal synthetic source fixture ${index}`),
  }));
  const digest = (index: number) => createHash("sha256").update(files[index].buffer).digest("hex");
  await page.route(site + "**", async route => route.fulfill(await staticResponse(route.request().url())));
  await page.route(backend + "/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/config")) return route.fulfill({ json: { mode: "email-gate" } });
    if (url.pathname.endsWith("/session")) return route.fulfill({ json: { token: "synthetic-save-bulk-capability" } });
    if (url.pathname.endsWith("/library")) return route.fulfill({ json: { papers: [...papers, ...additions], shared_saving_available: true, immediate_shared_saving: true } });
    if (url.pathname.endsWith("/source-uploads")) {
      const index = privateUploads++;
      return route.fulfill({ status: 201, json: { id: `upload-${index}`, state: "ready", digest: digest(index) } });
    }
    if (url.pathname.endsWith("/source-saves")) {
      expect(route.request().postData()).not.toContain('name="target"');
      const index = saves++;
      additions.push({ ...papers[0], sha256: digest(index), title: files[index].name });
      return route.fulfill({ status: 202, json: { id: `saved-${index}`, state: "saved", digest: digest(index) } });
    }
    if (url.pathname.endsWith("/jobs")) {
      comparisons++; submitted = route.request().postData() || "";
      return route.fulfill({ status: 202, json: { id: "saved-batch", status: "running", source_count: 94 } });
    }
    return route.fulfill({ json: { id: "saved-batch", status: "complete", checked: 94, total: 94, overlap_percent: 0 } });
  });
  await page.goto(site);
  await page.getByLabel("Email address").fill("fixture@example.org");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await page.getByLabel("Additional comparison papers").setInputFiles(files);
  await page.getByRole("button", { name: "Keep all selected PDFs in shared library", exact: true }).click();
  for (let index = 0; index < 50; index++) {
    await page.clock.runFor(3500);
    await expect(page.getByRole("status", { name: `Save status: Save ${index}.pdf`, exact: true })).toContainText("Saved to shared library.");
  }
  expect(saves).toBe(50); expect(comparisons).toBe(0);
  await expect(page.getByText("From the 94-paper comparison library", { exact: true })).toBeVisible();
  await page.reload();
  await page.getByLabel("Email address").fill("fixture@example.org");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  await expect(page.getByRole("heading", { name: "94 papers selected", exact: true })).toBeVisible();
  await page.getByLabel("Your paper", { exact: true }).setInputFiles({ name: "target.txt", mimeType: "text/plain", buffer: Buffer.from("Original manuscript.") });
  await page.getByRole("checkbox", { name: /authorized to upload/ }).check();
  await page.getByRole("button", { name: "Compare papers", exact: true }).click();
  await page.clock.runFor(1500);
  await expect(page.getByText("94 / 94", { exact: true })).toBeVisible();
  expect(comparisons).toBe(1);
  expect(submitted).toContain('name="uploaded_sources"\r\n\r\n[]');
});

test("bulk appends, same-name contents, per-file rejection, cancellation and 429 retry", async ({ page }) => {
  await page.clock.install();
  let uploads = 0, limited = true;
  const contentDigest = "a".repeat(64);
  await page.route(site + "**", async route => route.fulfill(await staticResponse(route.request().url())));
  await page.route(backend + "/**", async route => {
    const url = new URL(route.request().url());
    if (url.pathname.endsWith("/config")) return route.fulfill({ json: { mode: "email-gate" } });
    if (url.pathname.endsWith("/session")) return route.fulfill({ json: { token: "synthetic-append-capability" } });
    if (url.pathname.endsWith("/library")) return route.fulfill({ json: { papers } });
    if (url.pathname.endsWith("/source-uploads")) {
      uploads++;
      if (limited) { limited = false; return route.fulfill({ status: 429, headers: { "Retry-After": "10", "Access-Control-Expose-Headers": "Retry-After" }, json: { detail: "Queue busy." } }); }
      return route.fulfill({ status: 201, json: { id: `upload-${uploads}`, state: "ready", digest: uploads === 3 ? "b".repeat(64) : contentDigest } });
    }
    return route.fulfill({ json: { state: "deleted" } });
  });
  await page.goto(site);
  await page.getByLabel("Email address").fill("fixture@example.org");
  await page.getByRole("button", { name: "Continue", exact: true }).click();
  const file = (name: string, content: string) => ({ name, mimeType: "application/pdf", buffer: Buffer.from(content) });
  await page.getByLabel("Additional comparison papers").setInputFiles([
    file("same.pdf", "%PDF-1.7\nFirst original"), file("same.pdf", "%PDF-1.7\nOther original"),
    file("bad.pdf", "not a PDF"), file("cancel.pdf", "%PDF-1.7\ncancel queued")]);
  await expect(page.getByRole("button", { name: /^Remove comparison/ })).toHaveCount(4);
  await expect.poll(() => uploads).toBe(1);
  await page.getByLabel("Additional comparison papers").setInputFiles([file("copy.pdf", "%PDF-1.7\nFirst original")]);
  await expect(page.getByRole("button", { name: /^Remove comparison/ })).toHaveCount(5);
  await page.getByLabel("Additional comparison papers").setInputFiles([file("six.pdf", "%PDF-1.7\nsixth")]);
  await expect(page.getByRole("button", { name: /^Remove comparison/ })).toHaveCount(6);
  await expect(page.getByRole("status", { name: "Upload status: bad.pdf", exact: true })).toContainText("Upload failed");
  await page.getByRole("button", { name: "Remove comparison cancel.pdf", exact: true }).click();
  await page.clock.runFor(9000);
  expect(uploads).toBe(1);
  await page.clock.runFor(12000);
  await expect(page.getByRole("status", { name: "Upload status: same.pdf", exact: true }).first()).toContainText("Ready for comparison");
  await page.clock.runFor(2200);
  await expect(page.getByRole("status", { name: "Upload status: same.pdf", exact: true }).nth(1)).toContainText("Ready for comparison");
  await page.clock.runFor(2200);
  await expect(page.getByRole("status", { name: "Upload status: copy.pdf", exact: true })).toContainText("Identical content");
  await expect(page.getByRole("status", { name: "Upload status: same.pdf", exact: true })).toHaveCount(2);
  await page.getByLabel("Additional comparison papers").setInputFiles(Array.from({ length: 46 }, (_, index) => file(`extra${index}.pdf`, "%PDF-1.7\nextra")));
  await expect(page.getByRole("alert")).toContainText("up to 50");
  await expect(page.getByRole("button", { name: /^Remove comparison/ })).toHaveCount(5);
});
