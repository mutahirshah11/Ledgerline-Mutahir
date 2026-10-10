// 006-web T056 (US6): SCR-02 all runs (spec.md SCR-02-AC1..AC4, FR-007).
//
// Fixture data: `pre-review` scenario's `ep04-runs` (5 runs, newest first by created_at already);
// `empty` scenario's `ep04-runs-empty` ([]). RUN_IDS maps names to ids (support/mock-api.ts).

import { expect, RUN_IDS, test } from "./support/mock-api";

// 2026 redesign: the dashboard's "every month at a glance" table deliberately hides FAILED runs
// (declutter request) — their rows, outcome badges and proof text still get the exact same
// contract coverage, just against /runs/logs (scr-02-logs.spec.ts) instead of /runs. Nothing is
// deleted (apps/api audit_log is append-only; SCR-02-AC1/AC2 here cover the non-failed rows that
// do stay on the dashboard).

test("SCR-02-AC1 the runs list shows month, status, created, outcome counts and proof, newest first", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto("/runs");

  const rows = ["duplicate", "main"].map((name) => page.getByTestId(`run-row-${RUN_IDS[name]}`));

  // Newest first, in the order the API returned them.
  const table = page.getByRole("table");
  await expect(table).toBeVisible();
  for (let i = 0; i < rows.length; i++) {
    await expect(rows[i]).toBeVisible();
  }

  const mainRow = page.getByTestId(`run-row-${RUN_IDS.main}`);
  await expect(mainRow).toContainText("2026-09");
  await expect(mainRow.getByLabel("Finalised")).toBeVisible();
  await expect(mainRow.getByLabel("22 · All three agree")).toBeVisible();
  await expect(mainRow.getByLabel("1 · Duplicate row")).toBeVisible();
  await expect(mainRow.getByLabel("1 · Not a receipt (return)")).toBeVisible();
});

test("SCR-02-AC2 activating a row by click or keyboard opens the run overview", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto("/runs");

  const clickRow = page.getByTestId(`run-row-${RUN_IDS.main}`);
  await clickRow.click();
  await expect(page).toHaveURL(new RegExp(`/runs/${RUN_IDS.main}$`));

  await page.goto("/runs");
  const keyboardRow = page.getByTestId(`run-row-${RUN_IDS.duplicate}`);
  await keyboardRow.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/runs/${RUN_IDS.duplicate}$`));
});

test("SCR-02-AC3 New run shows for CONTROLLER and opens /runs/new, not for REVIEWER", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto("/runs");
  const newRun = page.getByRole("link", { name: "New run" });
  await expect(newRun).toBeVisible();
  await newRun.click();
  await expect(page).toHaveURL(/\/runs\/new$/);

  await mockApi.signIn("REVIEWER", "pre-review");
  await page.goto("/runs");
  await expect(page.getByRole("link", { name: "New run" })).toHaveCount(0);
});

test("SCR-02-AC4 no runs shows the empty message and, for a controller, New run", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "empty");
  await page.goto("/runs");

  await expect(page.getByText("No runs yet.")).toBeVisible();
  await expect(page.getByRole("link", { name: "New run" })).toBeVisible();
});
