import { expect, test } from "@playwright/test";

// 006-web T014: OutcomeBadge / RunStatusBadge render text + aria-label for every outcome and run
// status, via the test-only page app/__test__/badges/page.tsx (T023 gates it on
// ENABLE_TEST_PAGES=1, set only by the `screens` webServer in playwright.config.ts).
//
// NOTE for whoever builds middleware.ts/proxy.ts (T019, owned by another 006-web agent): the
// signed-out redirect to /login MUST exclude `/__test__/**`, or this test (and any other
// __test__ page) will never reach the page it is supposed to check. This test uses no auth.

const OUTCOME_CASES = [
  { testId: "outcome-0", text: "All three agree" },
  { testId: "outcome-1", text: "Agree after unit conversion" },
  { testId: "outcome-2", text: "ERP is different" },
  { testId: "outcome-3", text: "Sources disagree" },
  { testId: "outcome-4", text: "Missing from Register and Store sheet" },
  { testId: "outcome-5", text: "Needs review" },
  { testId: "outcome-6", text: "No GRN to match on" },
  { testId: "outcome-7", text: "Duplicate row" },
  { testId: "outcome-8", text: "Not a receipt (return)" },
  { testId: "outcome-9", text: "21 · All three agree" },
];

const RUN_STATUS_CASES = [
  { testId: "run-status-0", text: "Created" },
  { testId: "run-status-1", text: "Files uploaded" },
  { testId: "run-status-2", text: "Files checked" },
  { testId: "run-status-3", text: "Reading pages" },
  { testId: "run-status-4", text: "Pages read" },
  { testId: "run-status-5", text: "Reconciled" },
  { testId: "run-status-6", text: "Waiting on review" },
  { testId: "run-status-7", text: "Ready to finalise" },
  { testId: "run-status-8", text: "Finalised" },
  { testId: "run-status-9", text: "Failed at reading pages" },
];

test.beforeEach(async ({ page }) => {
  await page.goto("/__test__/badges");
});

for (const { testId, text } of OUTCOME_CASES) {
  test(`badges: OutcomeBadge ${testId} shows "${text}" as text and aria-label`, async ({ page }) => {
    const cell = page.getByTestId(testId);
    await expect(cell).toHaveText(text);
    await expect(cell.getByLabel(text, { exact: true })).toBeVisible();
  });
}

for (const { testId, text } of RUN_STATUS_CASES) {
  test(`badges: RunStatusBadge ${testId} shows "${text}" as text and aria-label`, async ({ page }) => {
    const cell = page.getByTestId(testId);
    await expect(cell).toHaveText(text);
    await expect(cell.getByLabel(text, { exact: true })).toBeVisible();
  });
}
