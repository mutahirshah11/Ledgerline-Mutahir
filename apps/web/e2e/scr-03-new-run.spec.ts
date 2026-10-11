import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import type { Page } from "@playwright/test";

import { RUN_IDS, expect, packFile, test } from "./support/mock-api";

// 006-web T031: SCR-03 "Start a run" (spec.md US2, SCR-03-AC1..AC12, FR-008..FR-015) against the
// recorded pack fixtures (scenario `new-run` unless stated). Read cost (spec Clarifications
// 2026-10-07) is asserted in SCR-03-AC5 (estimate) and SCR-03-COST (actual, unset prices).
// Upload retry for FM-01/FM-02 is SCR-03-FM02. No sleeps: web-first assertions, server-side
// `delayMs` to hold a step in flight, `page.clock` for the EP-19 poll.

const MAIN = RUN_IDS.main;
const PACK_HASHES = JSON.parse(
  readFileSync(resolve(__dirname, "..", "..", "api", "tests", "pack_hashes.json"), "utf8"),
) as Record<string, string>;

async function addPack(page: Page) {
  await page.locator('input[type="file"][name="register"]').setInputFiles([
    packFile("t4_register_page1.jpg"),
    packFile("t4_register_page2.jpg"),
  ]);
  await page.locator('input[type="file"][name="erp"]').setInputFiles(packFile("t4_erp_grn.csv"));
  await page.locator('input[type="file"][name="sheet"]').setInputFiles(packFile("t4_store_sheet.xlsx"));
}

async function openAndRun(page: Page) {
  await page.goto("/runs/new");
  await expect(page.getByLabel("Month")).toHaveValue("2026-09");
  await addPack(page);
  await page.getByRole("button", { name: "Start reconciliation", exact: true }).click();
}

// Some tests end while a delayed step request is still in flight (held on purpose so the screen
// stays put); drop those route callbacks instead of failing the worker with "Test ended".
test.afterEach(async ({ page }) => {
  await page.unrouteAll({ behavior: "ignoreErrors" });
});

const tracker = (page: Page) => page.getByRole("list", { name: "Steps" });
const stepItem = (page: Page, label: string) =>
  tracker(page).getByRole("listitem").filter({ has: page.getByText(label, { exact: true }) });

test("SCR-03-AC1 month picker defaults to the month before the scenario clock; three drop zones", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "new-run");
  await page.goto("/runs/new");
  await expect(page.getByRole("heading", { name: "Reconcile a new month" })).toBeVisible();
  await expect(page.getByLabel("Month")).toHaveValue("2026-09"); // EP-01 scenario_now 2026-10-15
  await expect(page.getByRole("group", { name: "Register pages" })).toBeVisible();
  await expect(page.getByRole("group", { name: "ERP" })).toBeVisible();
  await expect(page.getByRole("group", { name: "Store sheet" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Start reconciliation", exact: true })).toBeDisabled();
  expect(await mockApi.count("EP-01")).toBeGreaterThanOrEqual(1);
});

test("SCR-03-AC2 a file breaking a rule shows its problem inline and Run stays disabled", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "new-run");
  await page.goto("/runs/new");
  const run = page.getByRole("button", { name: "Start reconciliation", exact: true });
  const register = page.getByRole("group", { name: "Register pages" });
  await addPack(page);
  await expect(run).toBeEnabled();

  await page.locator('input[type="file"][name="register"]').setInputFiles([
    { name: "receipt.gif", mimeType: "image/gif", buffer: Buffer.alloc(40 * 1024) },
    { name: "tiny.jpg", mimeType: "image/jpeg", buffer: Buffer.alloc(1024) },
  ]);
  await expect(register.getByRole("listitem").filter({ hasText: "receipt.gif" })).toContainText(
    "Only JPEG or PNG photos",
  );
  await expect(register.getByRole("listitem").filter({ hasText: "tiny.jpg" })).toContainText(
    "Photo is under 20 KB",
  );
  await expect(run).toBeDisabled();

  await page.locator('input[type="file"][name="erp"]').setInputFiles({
    name: "second.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("a,b\n"),
  });
  await expect(page.getByRole("group", { name: "ERP" })).toContainText("Add exactly one ERP CSV");

  await register.getByRole("button", { name: "Remove receipt.gif" }).click();
  await register.getByRole("button", { name: "Remove tiny.jpg" }).click();
  await page.getByRole("group", { name: "ERP" }).getByRole("button", { name: "Remove second.csv" }).click();
  await expect(register).not.toContainText("Only JPEG or PNG photos");
  await expect(run).toBeEnabled();
  expect(await mockApi.count("EP-03")).toBe(0);
});

test("SCR-03-AC3 register pages are numbered in drop order and reorder by keyboard", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "new-run");
  await page.goto("/runs/new");
  await addPack(page);
  const pages = page.getByRole("list", { name: "Register pages in order" }).getByRole("listitem");
  await expect(pages).toHaveCount(2);
  await expect(pages.nth(0)).toContainText("Page 1");
  await expect(pages.nth(0)).toContainText("t4_register_page1.jpg");
  await expect(pages.nth(1)).toContainText("Page 2");

  await page.getByRole("button", { name: "Move down page 1" }).focus();
  await page.keyboard.press("Enter");
  await expect(pages.nth(0)).toContainText("t4_register_page2.jpg");
  await expect(pages.nth(0)).toContainText("Page 1");
  await expect(pages.nth(1)).toContainText("t4_register_page1.jpg");
  await page.getByRole("button", { name: "Move up page 2" }).focus();
  await page.keyboard.press("Space");
  await expect(pages.nth(0)).toContainText("t4_register_page1.jpg");
  await page.getByRole("button", { name: "Move down page 1" }).press("Enter");

  // The number is a label only: EP-06 declares page_no from the list order.
  await page.getByRole("button", { name: "Start reconciliation", exact: true }).click();
  await expect.poll(() => mockApi.count("EP-06")).toBe(1);
  const [ep06] = await mockApi.calls({ endpoint: "EP-06" });
  const files = (ep06.body as { files: { filename: string; page_no: number | null }[] }).files;
  expect(files.filter((f) => f.page_no !== null).map((f) => [f.page_no, f.filename])).toEqual([
    [1, "t4_register_page2.jpg"],
    [2, "t4_register_page1.jpg"],
  ]);
});

test("SCR-03-AC4 Run creates, hashes, uploads, confirms, validates, reads each page, reconciles in order", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "new-run");
  await mockApi.use({ "EP-08": { fixture: "ep08-pass", delayMs: 1500 } });
  await openAndRun(page);

  await expect(tracker(page).getByRole("listitem")).toHaveText([
    /Upload/,
    /Validate/,
    /Read page 1/,
    /Read page 2/,
    /Reconcile/,
  ]);
  await expect(page.locator('[aria-current="step"]')).toContainText("Validate");
  await expect(page).toHaveURL(new RegExp(`/runs/${MAIN}$`));

  const calls = (await mockApi.calls()).filter((c) => c.endpoint !== "AUTH" && c.endpoint !== "EP-02");
  const order = calls
    .filter((c) => c.endpoint !== "EP-01" && c.endpoint !== "EP-19" && c.endpoint !== "EP-05")
    .map((c) => (c.endpoint === "EP-09" ? `EP-09 ${c.path.split("/")[4]}` : c.endpoint));
  const firstAfterUpload = order.indexOf("EP-07");
  expect(order.slice(0, 2)).toEqual(["EP-03", "EP-06"]);
  expect(order.slice(2, firstAfterUpload)).toEqual(["STORAGE", "STORAGE", "STORAGE", "STORAGE"]);
  expect(order.slice(firstAfterUpload, firstAfterUpload + 5)).toEqual([
    "EP-07",
    "EP-08",
    "EP-09 1",
    "EP-09 2",
    "EP-10",
  ]);
  const [ep03] = await mockApi.calls({ endpoint: "EP-03" });
  expect(ep03.body).toEqual({ month: "2026-09" });
  const [ep06] = await mockApi.calls({ endpoint: "EP-06" });
  const declared = (ep06.body as { files: { filename: string; sha256: string; source: string }[] }).files;
  for (const f of declared) expect(f.sha256).toBe(PACK_HASHES[f.filename]);
  expect(declared.map((f) => f.source).sort()).toEqual(["ERP", "REGISTER", "REGISTER", "SHEET"]);
});

test("SCR-03-AC5 upload shows Uploading 4 files, then new/known pages and the estimated read cost", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "new-run");
  await mockApi.use({
    "EP-07": { fixture: "ep07-new-pages", delayMs: 1500 },
    "EP-10": { fixture: "ep10-reconcile-pass", delayMs: 3000 },
  });
  await openAndRun(page);
  const upload = stepItem(page, "Upload");
  await expect(upload).toContainText("Uploading 4 files");
  await expect(upload).toContainText("2 new pages to read");
  await expect(page.getByRole("region", { name: "Read cost" })).toContainText("2 new pages · est. US$0.023");
});

test("SCR-03-AC6 validation: a tick per source when it passes; every problem under its source when it fails", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "new-run");
  await mockApi.use({ "EP-10": { fixture: "ep10-reconcile-pass", delayMs: 3000 } });
  await openAndRun(page);
  const checks = page.getByRole("list", { name: "File checks" });
  await expect(checks.getByRole("listitem")).toHaveText(["✓ Register pages: OK", "✓ ERP: OK", "✓ Store sheet: OK"]);
  await expect(stepItem(page, "Read page 2")).toContainText("done");

  await mockApi.reset();
  await mockApi.signIn("CONTROLLER", "validation-fail");
  await openAndRun(page);
  const readsBefore = await mockApi.count("EP-09");
  const sheet = page.getByRole("list", { name: "File checks" }).getByRole("listitem").filter({ hasText: "Store sheet" });
  await expect(sheet).toContainText("Store sheet: no worksheet named Aug-2026 (found: Sep-2026)");
  await expect(stepItem(page, "Validate")).toContainText("Failed");
  await expect(stepItem(page, "Read page 1")).toContainText("Waiting");
  expect(await mockApi.count("EP-09")).toBe(readsBefore);
});

test("SCR-03-AC7 each page read says reused or new read with rows and cells needing review", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "new-run");
  await mockApi.use({
    "EP-09": ["ep09-cached-page-1", "ep09-new-read-page-2"],
    "EP-10": { fixture: "ep10-reconcile-pass", delayMs: 3000 },
  });
  await openAndRun(page);
  await expect(stepItem(page, "Read page 1")).toContainText("Page 1 of 2: same photo as an earlier run, its AI read reused (14 rows, 0 cells need you)");
  await expect(stepItem(page, "Read page 2")).toContainText(
    "Reading page 2 of 2… done (new read, 14 rows, 2 cells need you)",
  );
});

test("SCR-03-AC8 a failed step turns red with the message; Retry this step re-calls only that step", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "new-run");
  await mockApi.use({
    "EP-09": ["ep09-new-read-page-1", "ep09-upstream-failed", "ep09-new-read-page-2"],
    "EP-10": [{ status: 504, body: "Gateway Timeout" }, "ep10-reconcile-pass"],
  });
  await openAndRun(page);

  // 502 UPSTREAM_FAILED on page 2: red with the API's message, Reconcile still waiting.
  const page2 = stepItem(page, "Read page 2");
  await expect(page2).toContainText("Failed");
  await expect(page2).toContainText("openai failed after retries");
  await expect(stepItem(page, "Reconcile")).toContainText("Waiting");
  await page2.getByRole("button", { name: "Retry this step" }).click();

  // Then reconcile times out (504), and the retry drops the connection.
  const reconcile = stepItem(page, "Reconcile");
  await expect(reconcile).toContainText("Timed out. Try this step again.");
  await page.route("**/api/v1/runs/*/reconcile", (route) => route.abort(), { times: 1 });
  await reconcile.getByRole("button", { name: "Retry this step" }).click();
  await expect(reconcile).toContainText("Ledgerline is not reachable right now");
  await reconcile.getByRole("button", { name: "Retry this step" }).click();
  await expect(page).toHaveURL(new RegExp(`/runs/${MAIN}$`));

  const ep09 = (await mockApi.calls({ endpoint: "EP-09" })).map((c) => c.path.split("/")[4]);
  expect(ep09).toEqual(["1", "2", "2"]);
  expect(await mockApi.count("EP-03")).toBe(1);
  expect(await mockApi.count("EP-06")).toBe(1);
  expect(await mockApi.count("EP-07")).toBe(1);
  expect(await mockApi.count("EP-08")).toBe(1);
  expect(await mockApi.count("EP-10")).toBe(2); // the aborted call never reached the API
});

test("SCR-03-AC9 DUPLICATE_RUN says the files were already finalised and links to that run", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "duplicate-run");
  await openAndRun(page);
  await expect(page.getByText("This exact month and files were already finalised")).toBeVisible();
  await expect(page.getByRole("link", { name: "Open that run" })).toHaveAttribute("href", `/runs/${MAIN}`);
  expect(await mockApi.count("EP-08")).toBe(0);
});

test("SCR-03-AC10 when reconcile returns the screen opens the run overview", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "new-run");
  await openAndRun(page);
  await expect(page).toHaveURL(new RegExp(`/runs/${MAIN}$`));
  expect(await mockApi.count("EP-10")).toBe(1);
});

test("SCR-03-AC11 reloading with ?run= resumes at the next unfinished step without re-uploading", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "extracting");
  await mockApi.use({ "EP-10": { fixture: "ep10-reconcile-pass", delayMs: 1500 } });
  await page.goto(`/runs/new?run=${MAIN}`);
  await expect(stepItem(page, "Upload")).toContainText("Done");
  await expect(stepItem(page, "Validate")).toContainText("Done");
  await expect(stepItem(page, "Read page 1")).toContainText("Done");
  await expect(stepItem(page, "Read page 2")).toContainText("done (new read, 14 rows, 2 cells need you)");
  await expect(page).toHaveURL(new RegExp(`/runs/${MAIN}$`));

  expect((await mockApi.calls({ endpoint: "EP-09" })).map((c) => c.path.split("/")[4])).toEqual(["2"]);
  for (const ep of ["EP-03", "EP-06", "STORAGE", "EP-07", "EP-08"]) expect(await mockApi.count(ep)).toBe(0);
  expect(await mockApi.count("EP-10")).toBe(1);
});

test("SCR-03-AC12 while a step is in flight, an EP-19 RETRY shows retrying (attempt n of 3)", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "new-run");
  await mockApi.use({
    "EP-09": ["ep09-new-read-page-1", { fixture: "ep09-new-read-page-2", delayMs: 3000 }],
    "EP-10": { fixture: "ep10-reconcile-pass", delayMs: 3000 },
    "EP-19": {
      body: [
        {
          id: "00000000-0000-4000-8000-0000000000f1",
          stage: "extract",
          status: "RETRY",
          at: "2026-10-15T08:30:40+05:00",
          duration_ms: null,
          detail: { attempt: 2, cause: "timeout", page_no: 2, variant: "A" },
        },
        {
          id: "00000000-0000-4000-8000-0000000000f2",
          stage: "extract",
          status: "STARTED",
          at: "2026-10-15T08:30:39+05:00",
          duration_ms: null,
          detail: { page_no: 2 },
        },
      ],
    },
  });
  await page.clock.install();
  await openAndRun(page);
  const page2 = stepItem(page, "Read page 2");
  await expect(page2).toContainText("Reading page 2 of 2…");
  await page.clock.runFor(2000);
  await expect(page2).toContainText("retrying (attempt 2 of 3)");
  await expect(page2).toContainText("done (new read, 14 rows, 2 cells need you)", { timeout: 10_000 });
  await expect(page2).not.toContainText("retrying");
  expect(await mockApi.count("EP-19")).toBeGreaterThanOrEqual(1);
});

test("SCR-03-COST read cost after reading, all pages cached, and prices not set", async ({ page, mockApi }) => {
  // Resume of a run that failed while reading: estimate and actual from EP-05 read_cost.
  await mockApi.signIn("CONTROLLER", "failed-extract");
  await page.goto(`/runs/new?run=${RUN_IDS["failed-extract"]}`);
  const cost = page.getByRole("region", { name: "Read cost" });
  await expect(cost).toContainText("2 new pages · est. US$0.023");
  await expect(cost).toContainText("Read cost US$0.011");
  await expect(stepItem(page, "Read page 2")).toContainText("Page 2: openai failed: timeout after 3 attempts");

  // Every page already read: no cost.
  await mockApi.reset();
  await mockApi.signIn("CONTROLLER", "new-run");
  const known = mockApi.fixture<Record<string, unknown>>("ep07-known-pages");
  await mockApi.use({
    "EP-07": { body: { ...known, read_cost_estimate_usd: "0.000" } },
    "EP-10": { fixture: "ep10-reconcile-pass", delayMs: 3000 },
  });
  await openAndRun(page);
  await expect(stepItem(page, "Upload")).toContainText("0 new pages to read · 2 seen in an earlier run, reused (no AI call)");
  await expect(cost).toContainText("US$0 · same photos as an earlier run, reads reused");
  await expect(cost).toContainText("Read cost US$0.011"); // EP-05 after reading

  // Prices unset: nothing that looks like a price.
  await mockApi.reset();
  await mockApi.signIn("CONTROLLER", "new-run");
  const fresh = mockApi.fixture<Record<string, unknown>>("ep07-new-pages");
  await mockApi.use({
    "EP-07": { body: { ...fresh, read_cost_estimate_usd: null } },
    "EP-05": "ep05-pre-review-no-prices",
    "EP-10": { fixture: "ep10-reconcile-pass", delayMs: 3000 },
  });
  await openAndRun(page);
  await expect(stepItem(page, "Read page 2")).toContainText("done");
  await expect(cost.getByText("Read cost not available")).toHaveCount(2);
  await expect(cost).not.toContainText("US$");
});

test("SCR-03-FM02 a changed or missing upload: Retry this step asks for a link for that file only", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "new-run");
  await mockApi.use({
    "EP-07": [
      {
        status: 422,
        body: {
          error: {
            code: "CONTRACT_VIOLATION",
            message: "Upload not verified (HASH_MISMATCH): t4_erp_grn.csv",
            stage: "upload",
            details: {
              problem: "HASH_MISMATCH",
              files: [
                {
                  file_id: "00000000-0000-4000-8000-000000000016",
                  filename: "t4_erp_grn.csv",
                  source: "ERP",
                  problem: "HASH_MISMATCH",
                },
              ],
            },
          },
        },
      },
      "ep07-new-pages",
    ],
  });
  await openAndRun(page);
  const upload = stepItem(page, "Upload");
  await expect(upload).toContainText("File changed during upload, upload it again");
  await expect(upload).toContainText("t4_erp_grn.csv");
  await upload.getByRole("button", { name: "Retry this step" }).click();
  await expect(page).toHaveURL(new RegExp(`/runs/${MAIN}$`));

  const ep06 = await mockApi.calls({ endpoint: "EP-06" });
  expect(ep06).toHaveLength(2);
  expect((ep06[1].body as { files: { filename: string }[] }).files.map((f) => f.filename)).toEqual([
    "t4_erp_grn.csv",
  ]);
  expect(await mockApi.count("STORAGE", { method: "PUT" })).toBe(5);
  expect(await mockApi.count("EP-03")).toBe(1);
  expect(await mockApi.count("EP-07")).toBe(2);
});
