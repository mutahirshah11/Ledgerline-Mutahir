// 2026 redesign: /runs/logs is the full, unfiltered run history — the dashboard (/runs) hides
// FAILED runs to stay uncluttered (user request), but nothing is ever deleted (apps/api
// audit_log is append-only), so every run, including failed/old ones, is still reachable here.
// This file carries the SCR-02-AC1/AC2 coverage for FAILED-status rows that moved off /runs.

import { expect, RUN_IDS, test } from "./support/mock-api";

test("Logs shows every run, including failed ones the dashboard hides", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto("/runs/logs");

  const rows = ["validation-fail", "duplicate", "failed-proof", "main", "failed-extract"].map((name) =>
    page.getByTestId(`run-row-${RUN_IDS[name]}`),
  );

  const table = page.getByRole("table");
  await expect(table).toBeVisible();
  for (let i = 0; i < rows.length; i++) {
    await expect(rows[i]).toBeVisible();
  }

  const failedProofRow = page.getByTestId(`run-row-${RUN_IDS["failed-proof"]}`);
  await expect(failedProofRow).toContainText("Proof failed");
});

test("Logs rows open the run overview by click or keyboard, same as the dashboard", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto("/runs/logs");

  const keyboardRow = page.getByTestId(`run-row-${RUN_IDS["failed-proof"]}`);
  await keyboardRow.focus();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/runs/${RUN_IDS["failed-proof"]}$`));
});

test("Logs is reachable from the sidebar", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto("/runs");

  await page.getByRole("link", { name: "Logs", exact: true }).click();
  await expect(page).toHaveURL(/\/runs\/logs$/);
});
