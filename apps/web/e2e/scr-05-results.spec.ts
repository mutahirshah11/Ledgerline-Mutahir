import { expect, RUN_IDS, test } from "./support/mock-api";

// 006-web T051: SCR-05 results screen (US5; FR-023/FR-024). One test per acceptance ID. Every
// expected value comes from the recorded pack (e2e/fixtures/pack/ep11-groups*.json,
// ep21-norm-log*.json): 29 groups total in the `pre-review` scenario's EP-11 "groups" fixture;
// `?outcome=MISMATCHED` picks the 4-row "groups-mismatched" fixture (GRN 4412, 4415, 4417,
// 4425); GRN 4425's only diff is `qty` with outlier SHEET (REGISTER recorded "1200 YD" over its
// compared "1097.28 M"); GRN 4416's SHEET row is `XLS-r18`, whose only EP-21 entries are the
// three normalisation lines on that row (date/supplier/unit), the date one being
// DATE_TEXT_PARSED "9/14/2026" -> "2026-09-14".

const MAIN = RUN_IDS.main;
const RESULTS = `/runs/${MAIN}/results`;

const row = (page: import("@playwright/test").Page, grn: number) => page.getByTestId(`result-row-${grn}`);

test("SCR-05-AC1 one row per group, GRN/supplier/item/outcome and Register/ERP/Store sheet side by side", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(RESULTS);
  await expect(page.getByTestId("results-table")).toBeVisible();
  await expect(page.locator('[data-testid^="result-row-"]')).toHaveCount(29);

  const r4401 = row(page, 4401);
  await expect(r4401).toContainText("4401");
  await expect(r4401).toContainText("S-101");
  await expect(r4401).toContainText("Cartons 5-ply");
  await expect(r4401.getByText("Register")).toBeVisible();
  await expect(r4401.getByText("ERP")).toBeVisible();
  await expect(r4401.getByText("Store sheet")).toBeVisible();

  // GRN 4412's one diff is the lot number; its outlier (ERP) is marked in text, not colour alone.
  const r4412 = row(page, 4412);
  await expect(r4412).toContainText("ERP is different");
  await expect(r4412.getByText("(different)")).toBeVisible();
});

test("SCR-05-AC2 a converted value shows the recorded value and the compared value beneath it", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(RESULTS);
  const r4425 = row(page, 4425);
  await expect(r4425).toContainText("1200 YD");
  await expect(r4425).toContainText("1097.28 M");
  await expect(r4425).toContainText("Store sheet is different");
});

test("SCR-05-AC3 the outcome filter calls EP-11 with ?outcome= and the GRN search narrows the list", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(RESULTS);
  await expect(page.locator('[data-testid^="result-row-"]')).toHaveCount(29);

  await page.getByTestId("outcome-filter").selectOption("MISMATCHED");
  await expect(page.locator('[data-testid^="result-row-"]')).toHaveCount(4);
  const calls = await mockApi.calls({ endpoint: "EP-11" });
  expect(calls.some((c) => c.query.outcome === "MISMATCHED")).toBe(true);
  for (const grn of [4412, 4415, 4417, 4425]) {
    await expect(row(page, grn)).toBeVisible();
  }

  await page.getByTestId("outcome-filter").selectOption("");
  await expect(page.locator('[data-testid^="result-row-"]')).toHaveCount(29);

  await page.getByTestId("grn-search").fill("4416");
  await expect(page.locator('[data-testid^="result-row-"]')).toHaveCount(1);
  await expect(row(page, 4416)).toBeVisible();
});

test("SCR-05-AC4 expanding a row shows each source's raw values and its normalisation log in plain words", async ({
  page,
  mockApi,
}) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(RESULTS);
  await page.getByTestId("grn-search").fill("4416");
  await expect(row(page, 4416)).toBeVisible();

  await row(page, 4416).click();
  const expanded = page.getByTestId("result-expand-4416");
  await expect(expanded).toBeVisible();
  await expect(expanded.getByTestId("expand-SHEET")).toContainText("Store sheet date was text 9/14/2026, read as 14 Sep");
  await expect(expanded.getByTestId("expand-REGISTER")).toContainText("Qty");
  await expect(expanded.getByTestId("expand-ERP")).toContainText("Qty");
});

test("SCR-05-AC5 Export CSV downloads a CSV of the visible (filtered) rows", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto(RESULTS);
  await page.getByTestId("outcome-filter").selectOption("MISMATCHED");
  await expect(page.locator('[data-testid^="result-row-"]')).toHaveCount(4);

  const [download] = await Promise.all([
    page.waitForEvent("download"),
    page.getByTestId("export-csv").click(),
  ]);
  const stream = await download.createReadStream();
  const chunks: Buffer[] = [];
  for await (const chunk of stream!) chunks.push(chunk as Buffer);
  const csv = Buffer.concat(chunks).toString("utf8");

  const lines = csv.split("\r\n").filter(Boolean);
  expect(lines[0]).toBe("GRN,Outcome,Item,Supplier,Outlier,Register,ERP,Store sheet");
  expect(lines).toHaveLength(5); // header + 4 mismatched rows
  const fabricLine = lines.find((l) => l.startsWith("4425,"));
  expect(fabricLine).toContain("Store sheet is different");
  expect(fabricLine).toContain('"Grey fabric 63"""');
});
