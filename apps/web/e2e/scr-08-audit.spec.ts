import { expect, RUN_IDS, test } from "./support/mock-api";

// 006-web T060: SCR-08 `/runs/[id]/audit` (US7; FR-032). One test per acceptance ID. Rows and
// values come from the recorded fixtures (e2e/fixtures/pack/ep20-audit-post-review.json,
// ep19-events-pre-review.json), never invented here.

const MAIN = RUN_IDS.main;

test("SCR-08-AC1 a REVIEW_RESOLVED row shows before and after values", async ({ page, mockApi }) => {
  await mockApi.signIn("REVIEWER", "post-review");
  await page.goto(`/runs/${MAIN}/audit`);

  // ep20-audit-post-review.json id 30: CELL:REG-p2-r14:qty, ENTER_VALUE, resolved_value "250",
  // before read_a/read_b both ILLEGIBLE.
  const row = page.getByTestId("audit-row-30");
  await expect(row).toContainText("Review decision");
  await expect(row).toContainText("reviewer@kestrel.example");
  await expect(page.getByTestId("audit-before-30")).toContainText("ILLEGIBLE");
  await expect(page.getByTestId("audit-after-30")).toContainText("250");
});

test("SCR-08-AC2 the run-event timeline shows stage, status, time and duration, newest first", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(`/runs/${MAIN}/audit`);

  // ep19-events-pre-review.json: first element is the newest (reconcile OK, duration 0 ms), last
  // is the oldest (upload STARTED, duration null -> "—").
  const newest = page.locator("#main").getByTestId("event-row-00000000-0000-4000-8000-00000000003b");
  await expect(newest).toContainText("reconciling");
  await expect(newest).toContainText("OK");
  await expect(newest).toContainText("0 ms");

  const oldest = page.locator("#main").getByTestId("event-row-00000000-0000-4000-8000-000000000046");
  await expect(oldest).toContainText("uploading files");
  await expect(oldest).toContainText("Started");
  await expect(oldest).toContainText("—");

  const rows = page.locator("#main").getByTestId(/^event-row-/);
  await expect(rows).toHaveCount(12);
  await expect(rows.first()).toHaveAttribute("data-testid", "event-row-00000000-0000-4000-8000-00000000003b");
  await expect(rows.last()).toHaveAttribute("data-testid", "event-row-00000000-0000-4000-8000-000000000046");
});

test("SCR-08-AC3 the actor filter narrows the audit log to only that actor's rows", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "post-review");
  await page.goto(`/runs/${MAIN}/audit`);

  const allRows = page.getByTestId(/^audit-row-/);
  await expect(allRows).toHaveCount(17);

  await page.getByTestId("audit-actor-filter").selectOption("reviewer@kestrel.example");

  const filteredRows = page.getByTestId(/^audit-row-/);
  await expect(filteredRows).toHaveCount(4);
  for (const row of await filteredRows.all()) {
    await expect(row).toContainText("reviewer@kestrel.example");
  }
});
