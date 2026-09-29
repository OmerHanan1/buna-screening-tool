import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "hosted.spec.ts",
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  outputDir: "./test-results/hosted",
  use: { channel: "chromium", viewport: { width: 1440, height: 1000 }, acceptDownloads: true },
});
