import { expect, RUN_IDS, test } from "./support/mock-api";

// 006-web T038: SCR-04 run overview (US3; FR-016..FR-022, FR-036). One test per acceptance ID.
// Every expected exception line below is the recorded API text (e2e/fixtures/pack/ep05-*.json),
// never composed here (FR-017).

const MAIN = RUN_IDS.main;

/** The h2 section regions of the overview, top to bottom. */
async function sectionNames(page: import("@playwright/test").Page): Promise<string[]> {
  return page.getByRole("region").evaluateAll((els) =>
    els.map((el) => {
      const id = el.getAttribute("aria-labelledby");
      return (id ? document.getElementById(id)?.textContent : el.getAttribute("aria-label")) ?? "";
    }),
  );
}

test("SCR-04-AC1 headline reads the run's counts and the read cost sits beside it", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(`/runs/${MAIN}`);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("29 receipts: 21 match, 8 need attention");
  await expect(page.getByText("Read cost US$0.011", { exact: true })).toBeVisible();
});

test("SCR-04-AC2 proof badge shows rows placed once on PASS and the failed check on FAIL", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(`/runs/${MAIN}`);
  const pass = page.getByTestId("proof-badge");
  await expect(pass).toHaveText("Proof: 86 of 86 rows placed once");
  await expect(pass).toHaveAttribute("data-status", "PASS");

  await mockApi.scenario("failed-proof");
  await page.goto(`/runs/${RUN_IDS["failed-proof"]}`);
  const fail = page.getByTestId("proof-badge");
  await expect(fail).toHaveText("Proof failed: Every row read was placed");
  await expect(fail).toHaveAttribute("data-status", "FAIL");
});

test("SCR-04-AC3 sections in order with the API's lines verbatim and in order", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(`/runs/${MAIN}`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  expect(await sectionNames(page)).toEqual([
    "Act on these",
    "Sources disagree",
    "Waiting on review",
    "Control flags",
    "Also",
    "Finalise",
  ]);
  const lines = (name: string) => page.getByRole("region", { name }).getByTestId("exception-line");
  await expect(lines("Act on these")).toHaveText([
    "4421 stretch film, 75 ROLL: in register + sheet, NOT in ERP",
    "4430 cartons 5-ply, 1900 PCS: ERP only, dated 29 Sep, keyed 2 Oct by admin2",
  ]);
  await expect(lines("Sources disagree")).toHaveText([
    "4417 qty: ERP 46 CTN, register + sheet 48 CTN",
    "4425 qty: sheet 1200 M, register 1200 YD = ERP 1097.28 M",
    "4412 lot: ERP L2609-021, register + sheet L2609-012",
    "4415 date: ERP 13 Sep (Sun), register + sheet 12 Sep",
  ]);
  const waiting = page.getByRole("region", { name: "Waiting on review" });
  await expect(lines("Waiting on review")).toHaveText([
    "4428 qty unreadable on register (ink blot)",
    "4419 qty corrected on register, 60 to 66",
  ]);
  await expect(waiting.getByText("4 items waiting")).toBeVisible();
  await expect(waiting.getByRole("link", { name: "Review now" })).toHaveAttribute("href", `/runs/${MAIN}/review`);
  await expect(lines("Control flags")).toHaveText(["GRN 4429 missing everywhere"]);
  await expect(lines("Also")).toHaveText(["Sheet lists 4409 twice", "Sheet lists one return (-5000 PCS)"]);
  await expect(page.getByRole("link", { name: "4421 stretch film, 75 ROLL: in register + sheet, NOT in ERP" })).toHaveAttribute(
    "href",
    `/runs/${MAIN}/results?grn=4421`,
  );
  for (const [name, href] of [
    ["See all receipts", `/runs/${MAIN}/results`],
    ["Proof", `/runs/${MAIN}/proof`],
    ["Audit", `/runs/${MAIN}/audit`],
  ]) {
    await expect(page.getByRole("link", { name, exact: true })).toHaveAttribute("href", href);
  }
});

test("SCR-04-AC4 flags and statuses in plain language, never raw codes", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(`/runs/${MAIN}`);
  const act = page.getByRole("region", { name: "Act on these" });
  await expect(act.getByText("Entered late and back-dated")).toBeVisible();
  await expect(act.getByText("Entered by an unusual user")).toBeVisible();
  await expect(page.getByRole("region", { name: "Sources disagree" }).getByText("Dated on a Sunday")).toBeVisible();
  const text = await page.locator("#main").innerText();
  expect(text).not.toMatch(
    /MISMATCHED|NEEDS_REVIEW|MATCHED_AFTER|AWAITING_REVIEW|BACKDATED_ENTRY|UNUSUAL_USER|SUNDAY_DATE|SEQUENCE_GAP|OUT_OF_ORDER|NON_RECEIPT|\bREGISTER\b|\bSHEET\b|\/ERP/,
  );
});

test("SCR-04-AC5 finalise asks with the open count, calls EP-17 and shows the sent time", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(`/runs/${MAIN}`);
  await page.getByRole("button", { name: "Finalise and send" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toContainText("4 review items are still open. Finalise and send the summary now?");
  expect(await mockApi.count("EP-17")).toBe(0);
  await dialog.getByRole("button", { name: "Finalise and send" }).click();
  await expect(page.getByText("Finalised. Summary sent to the controller at 08:31")).toBeVisible();
  const calls = await mockApi.calls({ endpoint: "EP-17" });
  expect(calls).toHaveLength(1);
  expect(calls[0].body).toEqual({ confirm: true });
  await expect(page.getByRole("button", { name: "Finalise and send" })).toHaveCount(0);
});

test("SCR-04-AC6 summary not delivered banner; Resend calls EP-17 once and the banner goes", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "summary-failed");
  await mockApi.use({ "EP-17": "ep17-sent" });
  await page.goto(`/runs/${RUN_IDS["summary-failed"]}`);
  await expect(page.getByText("Summary not delivered")).toHaveCount(1);
  await page.getByRole("button", { name: "Resend" }).click();
  await expect(page.getByText("Finalised. Summary sent to the controller at 08:31")).toBeVisible();
  await expect(page.getByText("Summary not delivered")).toHaveCount(0);
  expect(await mockApi.count("EP-17")).toBe(1);
});

test("SCR-04-AC7 finalised run is read-only and asks EP-18 only on download click", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "finalised");
  await page.goto(`/runs/${MAIN}`);
  await expect(page.getByText("This run is finalised and read-only.")).toBeVisible();
  await expect(page.getByText("Finalised. Summary sent to the controller at 08:31")).toBeVisible();
  await expect(page.getByRole("button", { name: "Finalise and send" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Review now" })).toHaveCount(0);
  const report = page.getByRole("button", { name: "Download report" });
  const csv = page.getByRole("button", { name: "Download outcomes CSV" });
  await expect(report).toBeVisible();
  await expect(csv).toBeVisible();
  expect(await mockApi.count("EP-18")).toBe(0);

  const htmlRequest = page.context().waitForEvent("request", (r) => r.url().includes("/report.html"));
  await report.click();
  await htmlRequest;
  expect(await mockApi.count("EP-18")).toBe(1);

  const csvRequest = page.context().waitForEvent("request", (r) => r.url().includes("/outcomes.csv"));
  await csv.click();
  await csvRequest;
  expect(await mockApi.count("EP-18")).toBe(2);
});

test("SCR-04-AC8 error banner with the message, failed stage and a Retry link", async ({ page, mockApi }) => {
  const id = RUN_IDS["failed-proof"];
  await mockApi.signIn("CONTROLLER", "failed-proof");
  await page.goto(`/runs/${id}`);
  const banner = page.getByRole("alert").filter({ hasText: "C1 failed" });
  await expect(banner).toContainText(
    "C1 failed: SHEET in 30, placed 29; C2 failed: unplaced [XLS-r30]; C4 failed: SHEET|BDL in 250, placed 0",
  );
  await expect(banner).toContainText("Failed at the completeness proof.");
  await expect(banner.getByRole("link", { name: "Retry" })).toHaveAttribute("href", `/runs/new?run=${id}`);
});

test("SCR-04-AC9 proof FAIL disables Finalise and send with the reason", async ({ page, mockApi }) => {
  const run = mockApi.fixture<Record<string, unknown>>("ep05-pre-review");
  await mockApi.signIn("CONTROLLER", "pre-review");
  await mockApi.use({ "EP-05": { status: 200, body: { ...run, proof_status: "FAIL" } }, "EP-13": "ep13-proof-fail" });
  await page.goto(`/runs/${MAIN}`);
  const button = page.getByRole("button", { name: "Finalise and send" });
  await expect(button).toBeDisabled();
  await expect(page.getByText("Proof has not passed")).toBeVisible();
  await expect(button).toHaveAccessibleDescription("Proof has not passed");
  expect(await mockApi.count("EP-17")).toBe(0);
});

test("SCR-04-AC10 usable at 380 px with no sideways page scroll", async ({ page, mockApi }) => {
  await page.setViewportSize({ width: 380, height: 800 });
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(`/runs/${MAIN}`);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  const button = page.getByRole("button", { name: "Finalise and send" });
  await button.scrollIntoViewIfNeeded();
  const box = await button.boundingBox();
  expect(box && box.x + box.width).toBeLessThanOrEqual(380);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(380);
});
