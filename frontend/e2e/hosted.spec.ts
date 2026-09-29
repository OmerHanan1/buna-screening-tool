import { test, expect } from "@playwright/test";
import fs from "node:fs/promises";
import path from "node:path";
import { spawnSync } from "node:child_process";

const site = process.env.HOSTED_SITE_URL || "https://kind-field-035b3910f.4.azurestaticapps.net/";
const backend = "https://paper-overlap-api.purpleflower-beedf5ce.eastus.azurecontainerapps.io";
const sitePath = new URL(site).pathname;
const staticConfig = JSON.parse(await fs.readFile("public/staticwebapp.config.json", "utf8"));

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
  await page.getByRole("button", { name: "Open PDF", exact: true }).click();
  const popup = await popupWait;
  await expect.poll(() => popup.url(), { timeout: 90_000 }).toMatch(/^blob:/);
  await popup.close();
  const downloadWait = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download PDF", exact: true }).click();
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
    await page.getByRole("button", { name: "Download PDF", exact: true }).click();
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
  await expect(page.getByText("Not assessed", { exact: true })).toBeVisible();
  await expect(page.getByText(/Comparison incomplete. No score is available/)).toBeVisible();
  await expect(page.getByText(/Abstract heading not detected/)).toBeVisible();
  expired = true;
  await page.getByRole("button", { name: "Download PDF", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("workspace expired");
  await expect(page.getByLabel("Email address")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Comparison report", exact: true })).toHaveCount(0);
});
