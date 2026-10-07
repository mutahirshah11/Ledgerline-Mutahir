import type { Page } from "@playwright/test";

import { expect, REVIEW_ITEM_IDS, RUN_IDS, test, type MockApi } from "./support/mock-api";

// 006-web T045: SCR-06 review queue (US4; FR-025..FR-030, FR-036). One test per acceptance ID.
// Every expected value comes from the recorded pack (e2e/fixtures/pack/ep14-*, ep15-*, ep16-*):
// EP-14 open items in API order 4419 qty (CORRECTED, both reads 66), 4428 qty (ILLEGIBLE, ink
// blot), 4430 BACKDATED_ENTRY flag, 4429 SEQUENCE_GAP flag; page 2 edges 0.199 / 0.964.

const MAIN = RUN_IDS.main;
const REVIEW = `/runs/${MAIN}/review`;
const ITEM = {
  i4419: REVIEW_ITEM_IDS["CELL:REG-p2-r05:qty"],
  i4428: REVIEW_ITEM_IDS["CELL:REG-p2-r14:qty"],
  i4430: REVIEW_ITEM_IDS["FLAG:BACKDATED_ENTRY:ERP-0028"],
  i4429: REVIEW_ITEM_IDS["FLAG:SEQUENCE_GAP:4429"],
};
/** The page 2 photo behind its signed link (ep16-page-2). */
const P2_IMAGE = /^http:\/\/storage\.test\/.*\/register\/p2-[0-9a-f]+\.jpg/;
const OPEN_SEQUENCE = [
  "ep14-open-pre-review",
  "ep14-open-after-1",
  "ep14-open-after-2",
  "ep14-open-after-3",
  "ep14-open-after-4",
];
const REASON_4419 = "4419 · Qty · Corrected on the register. Read A: 66. Read B: 66.";
const REASON_4428 =
  "4428 · Qty · Ink blot covers the quantity; leading character partially visible | leading 2 " +
  "visible, rest covered by ink blot. Read A: unreadable. Read B: unreadable.";

const card = (page: Page) => page.getByTestId("review-card");
const reason = (page: Page) => card(page).getByTestId("reason-line");
const progress = (page: Page) => page.getByTestId("review-progress");
const button = (page: Page, name: string) => card(page).getByRole("button", { name, exact: true });

async function resolveBodies(mockApi: MockApi): Promise<{ path: string; body: unknown }[]> {
  return (await mockApi.calls({ endpoint: "EP-15" })).map((c) => ({ path: c.path, body: c.body }));
}

/** The 4428 card's EP-14 list with the table edges taken out. */
function withoutEdges(mockApi: MockApi): unknown {
  const items = structuredClone(mockApi.fixture<Record<string, unknown>[]>("ep14-open-after-1"));
  const ctx = items[0].context as Record<string, unknown>;
  ctx.table_top = null;
  ctx.table_bottom = null;
  return items;
}

test("SCR-06-AC1 only open items, one card at a time, in the API's order with progress", async ({ page, mockApi }) => {
  await mockApi.signIn("REVIEWER", "pre-review");
  await mockApi.use({ "EP-14": OPEN_SEQUENCE });
  await page.goto(REVIEW);
  await expect(progress(page)).toHaveText("4 of 4 left");
  await expect(card(page)).toHaveCount(1);
  await expect(reason(page)).toHaveText(REASON_4419);
  const lists = await mockApi.calls({ endpoint: "EP-14" });
  expect(lists.map((c) => c.query)).toEqual([{ status: "OPEN" }]);

  // The next card is the API's next item (4428, then the flags 4430 and 4429).
  await button(page, "Accept read").click();
  await expect(progress(page)).toHaveText("3 of 4 left");
  await expect(reason(page)).toHaveText(REASON_4428);
  await expect(card(page)).toHaveCount(1);
});

test("SCR-06-AC2 cell card: reason in one line, full-width photo with the row band, photo and reads before other sources", async ({ page, mockApi }) => {
  await mockApi.signIn("REVIEWER", "pre-review");
  await mockApi.use({ "EP-14": "ep14-open-after-1" });
  await page.goto(REVIEW);
  await expect(reason(page)).toHaveText(REASON_4428);

  const img = card(page).getByRole("img", { name: "Register page 2" });
  await expect(img).toBeVisible();
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0);
  const cardBox = (await card(page).boundingBox())!;
  const imgBox = (await img.boundingBox())!;
  expect(imgBox.width).toBeGreaterThan(cardBox.width * 0.85);
  await expect(card(page).getByTestId("row-band")).toBeVisible();

  const readA = card(page).getByTestId("read-a");
  const readB = card(page).getByTestId("read-b");
  await expect(readA).toContainText("Read A");
  await expect(readA).toContainText("unreadable");
  await expect(readB).toContainText("unreadable");
  const order = await card(page).evaluate((el) => {
    const pick = (sel: string) => el.querySelector(sel)!;
    const before = (a: Element, b: Element) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
    const r = pick('[data-testid="reason-line"]');
    const photo = pick("img");
    const a = pick('[data-testid="read-a"]');
    const b = pick('[data-testid="read-b"]');
    const other = pick('[data-testid="other-sources"]');
    return [before(r, photo), before(photo, a), before(a, b), before(b, other)];
  });
  expect(order).toEqual([true, true, true, true]);
});

test("SCR-06-AC3 band covers slot row_index of 16 between the edges; no edges -> no band and Row 14 above the image", async ({ page, mockApi }) => {
  await mockApi.signIn("REVIEWER", "pre-review");
  await mockApi.use({ "EP-14": "ep14-open-after-1" });
  await page.goto(REVIEW);
  // ep16-page-2: table_top 0.199, table_bottom 0.964; row 14 -> 0.199 + 13 * 0.765 / 16.
  const band = card(page).getByTestId("row-band");
  const photo = card(page).getByRole("img", { name: "Register page 2" });
  await expect(band).toBeVisible();
  await expect.poll(() => photo.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0);
  const [b, p] = [(await band.boundingBox())!, (await photo.boundingBox())!];
  expect((b.y - p.y) / p.height).toBeCloseTo(0.8205625, 3);
  expect(b.height / p.height).toBeCloseTo(0.0478125, 3);
  await expect(card(page).getByText("Row 14", { exact: true })).toHaveCount(0);

  const link = { ...mockApi.fixture<Record<string, unknown>>("ep16-page-2"), table_top: null, table_bottom: null };
  await mockApi.use({ "EP-14": { body: withoutEdges(mockApi) }, "EP-16": { body: link } });
  await page.reload();
  await expect(reason(page)).toHaveText(REASON_4428);
  const fallback = card(page).getByText("Row 14", { exact: true });
  await expect(fallback).toBeVisible();
  await expect(card(page).getByTestId("row-band")).toHaveCount(0);
  const img = card(page).getByRole("img", { name: "Register page 2" });
  const above = await fallback.evaluate(
    (el, imgEl) => !!(el.compareDocumentPosition(imgEl!) & Node.DOCUMENT_POSITION_FOLLOWING),
    await img.elementHandle(),
  );
  expect(above).toBe(true);
});

test("SCR-06-AC4 ERP and sheet values under 'other sources, for reference' and the value input is empty", async ({ page, mockApi }) => {
  await mockApi.signIn("REVIEWER", "pre-review");
  await mockApi.use({ "EP-14": "ep14-open-after-1" });
  await page.goto(REVIEW);
  const other = card(page).getByRole("region", { name: "other sources, for reference" });
  await expect(other).toBeVisible();
  await expect(other.getByTestId("other-source")).toHaveText(["ERP 250 BDL", "Sheet 250 BDL"]);
  // Never pre-filled from another source (F-08.R3), even though ERP and sheet agree on 250.
  await expect(card(page).getByLabel("Value", { exact: true })).toHaveValue("");
  await expect(card(page).getByLabel("Note", { exact: true })).toHaveValue("");
});

test("SCR-06-AC5 each action sends EP-15 with its action and required fields", async ({ page, mockApi }) => {
  await mockApi.signIn("REVIEWER", "pre-review");
  await mockApi.use({ "EP-14": ["ep14-open-pre-review", "ep14-open-after-1"] });
  await page.goto(REVIEW);

  // 4419: both reads give 66 (corrected 60 -> 66), so Accept read is enabled; no Acknowledge.
  await expect(reason(page)).toHaveText(REASON_4419);
  await expect(button(page, "Accept read")).toBeEnabled();
  await expect(button(page, "Enter value")).toBeVisible();
  await expect(button(page, "Cannot determine")).toBeVisible();
  await expect(button(page, "Acknowledge")).toHaveCount(0);
  await expect(card(page).getByTestId("read-a")).toContainText("60 struck out, 66 written");
  await button(page, "Accept read").click();
  await expect(reason(page)).toHaveText(REASON_4428);
  expect(await resolveBodies(mockApi)).toEqual([
    { path: `/review-items/${ITEM.i4419}/resolve`, body: { action: "ACCEPT_READ" } },
  ]);

  // 4428: no read value, so Accept read is disabled; Enter value needs a value; Cannot determine
  // needs a note (checked before sending).
  await expect(button(page, "Accept read")).toBeDisabled();
  await button(page, "Cannot determine").click();
  await expect(card(page).getByText("A note is required")).toBeVisible();
  await button(page, "Enter value").click();
  await expect(card(page).getByText("A value is required")).toBeVisible();
  expect(await mockApi.count("EP-15")).toBe(1);

  await card(page).getByLabel("Value", { exact: true }).fill("250");
  await button(page, "Enter value").click();
  await expect.poll(() => mockApi.count("EP-15")).toBe(2);
  // The same item comes back (the list stays at after-1): a fresh card, input empty again.
  await expect(card(page).getByLabel("Value", { exact: true })).toHaveValue("");
  await card(page).getByLabel("Note", { exact: true }).fill("Ink blot; asked stores to recount.");
  await button(page, "Cannot determine").click();
  await expect.poll(() => mockApi.count("EP-15")).toBe(3);

  // 4429 (SEQUENCE_GAP flag): Acknowledge only, note optional.
  await mockApi.use({ "EP-14": "ep14-open-after-3" });
  await page.reload();
  await expect(reason(page)).toContainText("4429");
  await expect(button(page, "Acknowledge")).toBeVisible();
  for (const name of ["Accept read", "Enter value", "Cannot determine"]) {
    await expect(button(page, name)).toHaveCount(0);
  }
  await expect(card(page).getByLabel("Value", { exact: true })).toHaveCount(0);
  await button(page, "Acknowledge").click();
  await expect.poll(() => mockApi.count("EP-15")).toBe(4);

  expect((await resolveBodies(mockApi)).slice(1)).toEqual([
    { path: `/review-items/${ITEM.i4428}/resolve`, body: { action: "ENTER_VALUE", value: "250" } },
    {
      path: `/review-items/${ITEM.i4428}/resolve`,
      body: { action: "CANNOT_DETERMINE", note: "Ink blot; asked stores to recount." },
    },
    { path: `/review-items/${ITEM.i4429}/resolve`, body: { action: "ACKNOWLEDGE" } },
  ]);
});

test("SCR-06-AC6 a 422 shows inline on the card and the card stays; a bad qty is caught before sending", async ({ page, mockApi }) => {
  await mockApi.signIn("REVIEWER", "pre-review");
  await mockApi.use({ "EP-14": "ep14-open-after-1", "EP-15": "ep15-422-bad-qty" });
  await page.goto(REVIEW);
  const value = card(page).getByLabel("Value", { exact: true });

  // "abc" for qty: the same rule as the API (ASCII digits, optional fraction), so no call.
  await value.fill("abc");
  await button(page, "Enter value").click();
  await expect(card(page).getByRole("alert")).toHaveText("qty must be a number of 0 or more");
  expect(await mockApi.count("EP-15")).toBe(0);

  // The API refuses a value the screen let through: its message and reason, inline.
  await value.fill("250");
  await button(page, "Enter value").click();
  await expect(card(page).getByRole("alert")).toHaveText(
    "This action is not valid for this item: qty must be a number of 0 or more",
  );
  expect(await mockApi.count("EP-15")).toBe(1);
  await expect(reason(page)).toHaveText(REASON_4428);
  await expect(value).toHaveValue("250");
  expect(await mockApi.count("EP-14")).toBe(1);
});

test("SCR-06-AC7 409: the card shows the first decision and the queue refreshes", async ({ page, mockApi }) => {
  await mockApi.signIn("REVIEWER", "pre-review");
  await mockApi.use({
    "EP-14": ["ep14-open-pre-review", "ep14-open-after-1"],
    "EP-15": "ep15-409-already-resolved",
  });
  await page.goto(REVIEW);
  await expect(reason(page)).toHaveText(REASON_4419);
  await button(page, "Accept read").click();
  // ep15-409-already-resolved: resolved_by reviewer@kestrel.example, 2026-10-15T08:31:15+05:00, 66.
  await expect(card(page).getByRole("alert")).toHaveText(
    "Already decided by reviewer@kestrel.example at 2026-10-15 08:31: 66",
  );
  await expect(reason(page)).toHaveText(REASON_4419);
  await expect(button(page, "Accept read")).toHaveCount(0);
  await expect(progress(page)).toHaveText("3 of 4 left");
  expect(await mockApi.count("EP-14")).toBe(2);
  await page.getByRole("button", { name: "Next item" }).click();
  await expect(reason(page)).toHaveText(REASON_4428);
});

test("SCR-06-AC8 an expired image link is renewed once from EP-16 and the image reloads", async ({ page, mockApi }) => {
  await mockApi.signIn("REVIEWER", "pre-review");
  await mockApi.use({ "EP-14": "ep14-open-after-1" });
  let imageGets = 0;
  await page.route(P2_IMAGE, async (route) => {
    imageGets += 1;
    if (imageGets === 1) return route.fulfill({ status: 403, body: "expired" });
    return route.fallback();
  });
  await page.goto(REVIEW);
  const img = card(page).getByRole("img", { name: "Register page 2" });
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true);
  expect(imageGets).toBe(2);
  expect(await mockApi.count("EP-16", { path: `/runs/${MAIN}/pages/2/image` })).toBe(2);
  await expect(card(page).getByTestId("row-band")).toBeVisible();

  // A second failure does not loop: still exactly one renewal.
  await page.unroute(P2_IMAGE);
  await page.route(P2_IMAGE, (route) => route.fulfill({ status: 403, body: "expired" }));
  await page.reload();
  await expect(card(page).getByText("The page image could not be loaded.")).toBeVisible();
  expect(await mockApi.count("EP-16", { path: `/runs/${MAIN}/pages/2/image` })).toBe(4);
});

test("SCR-06-AC9 after each decision the next card shows and the count drops; then the queue is empty", async ({ page, mockApi }) => {
  await mockApi.signIn("REVIEWER", "pre-review");
  await mockApi.use({ "EP-14": OPEN_SEQUENCE });
  await page.goto(REVIEW);
  await expect(progress(page)).toHaveText("4 of 4 left");
  await button(page, "Accept read").click();
  await expect(progress(page)).toHaveText("3 of 4 left");
  await card(page).getByLabel("Value", { exact: true }).fill("250");
  await button(page, "Enter value").click();
  await expect(progress(page)).toHaveText("2 of 4 left");
  await expect(reason(page)).toContainText("4430");
  await button(page, "Acknowledge").click();
  await expect(progress(page)).toHaveText("1 of 4 left");
  await expect(reason(page)).toContainText("4429");
  await button(page, "Acknowledge").click();

  await expect(page.getByText("Nothing left to review.")).toBeVisible();
  await expect(card(page)).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Back to overview" })).toHaveAttribute("href", `/runs/${MAIN}`);
  // ep15-acknowledge-sequence-gap-4429 returns the run READY.
  await expect(page.getByLabel("Ready to finalise")).toBeVisible();
  expect((await resolveBodies(mockApi)).map((c) => c.path)).toEqual([
    `/review-items/${ITEM.i4419}/resolve`,
    `/review-items/${ITEM.i4428}/resolve`,
    `/review-items/${ITEM.i4430}/resolve`,
    `/review-items/${ITEM.i4429}/resolve`,
  ]);
});

test("SCR-06-AC10 a FINALISED run or an OPS person sees the queue read-only with the reason", async ({ page, mockApi }) => {
  // Finalised: ep05-finalised; show the pre-review items so a card is there to be read-only.
  await mockApi.signIn("CONTROLLER", "finalised");
  await mockApi.use({ "EP-14": "ep14-open-pre-review" });
  await page.goto(REVIEW);
  await expect(reason(page)).toHaveText(REASON_4419);
  await expect(page.getByText("This run is finalised and read-only.")).toBeVisible();
  await expect(card(page).getByRole("button")).toHaveCount(0);
  await expect(card(page).getByLabel("Value", { exact: true })).toHaveCount(0);

  // OPS on a run waiting for review.
  await mockApi.reset();
  await mockApi.signIn("OPS", "pre-review");
  await page.goto(REVIEW);
  await expect(reason(page)).toHaveText(REASON_4419);
  await expect(page.getByText("Your role can view but not decide.")).toBeVisible();
  await expect(card(page).getByRole("button")).toHaveCount(0);
  await expect(card(page).getByLabel("Note", { exact: true })).toHaveCount(0);
  expect(await mockApi.count("EP-15")).toBe(0);
});

test("SCR-06-AC11 at 380 px the image, reads, other sources and actions fit with no sideways scroll", async ({ page, mockApi }) => {
  await page.setViewportSize({ width: 380, height: 800 });
  await mockApi.signIn("REVIEWER", "pre-review");
  await mockApi.use({ "EP-14": "ep14-open-after-1" });
  await page.goto(REVIEW);
  await expect(reason(page)).toHaveText(REASON_4428);
  const img = card(page).getByRole("img", { name: "Register page 2" });
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(380);
  for (const loc of [
    img,
    card(page).getByTestId("read-a"),
    card(page).getByTestId("read-b"),
    card(page).getByRole("region", { name: "other sources, for reference" }),
    card(page).getByLabel("Value", { exact: true }),
    button(page, "Enter value"),
    button(page, "Cannot determine"),
  ]) {
    const box = (await loc.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(380);
  }
  // Image first, then reads, then actions (top to bottom).
  const imgBox = (await img.boundingBox())!;
  const readBox = (await card(page).getByTestId("read-a").boundingBox())!;
  const actionBox = (await button(page, "Enter value").boundingBox())!;
  expect(imgBox.y).toBeLessThan(readBox.y);
  expect(readBox.y).toBeLessThan(actionBox.y);
});
