import { expect, RUN_IDS, test } from "./support/mock-api";

// 006-web T059: SCR-07 `/runs/[id]/proof` (US7; FR-031). One test per acceptance ID. Numbers come
// from the recorded fixtures (e2e/fixtures/pack/ep13-proof-pass.json, ep13-proof-fail.json), never
// invented here.
//
// Assumption (blueprint ambiguity, documented per task instructions): spec.md's "qty unknown
// REG-p2-r14 pre-review, absent post-review" describes the review step that *resolves* a
// qty-unknown cell (ep20-audit-post-review.json's REVIEW_RESOLVED row for CELL:REG-p2-r14:qty,
// resolved_value "250"). EP-13 itself is a frozen read (computed once at the `reconcile` stage,
// before review) and the `pre-review`/`post-review` scenarios both serve the same
// ep13-proof-pass.json fixture, so this screen (server read of EP-13 + EP-05 only, per
// contracts/screens.md SCR-07) cannot show a different qty-unknown list between those two
// scenarios. AC3 below instead tests the one thing the fixture does prove: REG-p2-r14 is listed
// by id, never shown as a zero quantity.

const MAIN = RUN_IDS.main;
const FAILED_PROOF = RUN_IDS["failed-proof"];

test("SCR-07-AC1 checks C1 to C5 each show PASS or FAIL in text and colour with their meaning", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(`/runs/${MAIN}/proof`);

  for (const id of ["C1", "C2", "C3", "C4", "C5"] as const) {
    const check = page.getByTestId(`proof-check-${id}`);
    await expect(check).toHaveAttribute("data-status", "PASS");
    await expect(check).toContainText("PASS");
  }
  await expect(page.getByTestId("proof-check-C4")).toContainText("Quantity totals tie");

  await mockApi.scenario("failed-proof");
  await page.goto(`/runs/${FAILED_PROOF}/proof`);

  const failing: Record<string, "PASS" | "FAIL"> = { C1: "FAIL", C2: "FAIL", C3: "PASS", C4: "FAIL", C5: "PASS" };
  for (const [id, status] of Object.entries(failing)) {
    const check = page.getByTestId(`proof-check-${id}`);
    await expect(check).toHaveAttribute("data-status", status);
    await expect(check).toContainText(status);
  }
});

test("SCR-07-AC2 C1 per-source table shows rows in, rows placed and rows by outcome", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(`/runs/${MAIN}/proof`);

  await expect(page.getByTestId("proof-c1-row-ERP")).toHaveText(["ERP", "28", "28"].join(""));
  await expect(page.getByTestId("proof-c1-row-REGISTER")).toHaveText(["Register", "28", "28"].join(""));
  await expect(page.getByTestId("proof-c1-row-SHEET")).toHaveText(["Store sheet", "30", "30"].join(""));

  // EP-05 `counts` by outcome (pre-review fixture): MATCHED 21, MISMATCHED 4.
  await expect(page.getByText("21 · All three agree", { exact: true })).toBeVisible();
  await expect(page.getByText("4 · All three differ", { exact: true })).toBeVisible();
});

test("SCR-07-AC3 C4 per source|unit qty totals in vs placed, and qty-unknown rows listed by id, never as zero", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(`/runs/${MAIN}/proof`);

  await expect(page.getByTestId("proof-c4-row-ERP|BAG")).toHaveText(["ERP", "BAG", "125", "125"].join(""));
  await expect(page.getByTestId("proof-c4-row-REGISTER|CTNS")).toHaveText(["REGISTER", "CTNS", "48", "48"].join(""));

  const qtyUnknown = page.getByTestId("proof-c4-qty-unknown");
  await expect(qtyUnknown).toContainText("Quantity unknown (not counted as zero)");
  await expect(qtyUnknown).toContainText("REG-p2-r05");
  await expect(qtyUnknown).toContainText("REG-p2-r14");
  await expect(qtyUnknown).not.toContainText(": 0");
});

test("SCR-07-AC4 C5 shows the GRN count and the list of gaps", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(`/runs/${MAIN}/proof`);

  await expect(page.getByTestId("proof-c5-grns")).toHaveText("29 GRNs");
  await expect(page.getByTestId("proof-c5-gaps")).toHaveText("Gaps: 4429");
});
