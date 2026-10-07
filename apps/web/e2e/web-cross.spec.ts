import { expect, RUN_IDS, test } from "./support/mock-api";

// 006-web cross-screen acceptance tests (spec.md WEB-AC1..AC6). One test per acceptance ID.

const MAIN = RUN_IDS.main;

test("WEB-AC1 a 401 mid-review sends the person to sign-in and back, keeping the typed note", async ({ page, mockApi }) => {
  const review = `/runs/${MAIN}/review`;
  await mockApi.signIn("REVIEWER", "pre-review");
  await mockApi.use({ "EP-14": "ep14-open-after-1" }); // the 4428 card
  await page.goto(review);
  const card = page.getByTestId("review-card");
  await expect(card.getByTestId("reason-line")).toContainText("4428");

  const note = "Ink blot; asked stores to recount.";
  await card.getByLabel("Note", { exact: true }).fill(note);
  await mockApi.use({ "EP-15": { status: 401, body: mockApi.fixture("ep02-unauthenticated") } });
  await card.getByRole("button", { name: "Cannot determine", exact: true }).click();

  // lib/api.ts: local sign-out, then /login?next=<this page>&expired=1.
  await expect(page).toHaveURL(
    (url) => url.pathname === "/login" && url.searchParams.get("next") === review && url.searchParams.get("expired") === "1",
  );
  expect(await mockApi.count("EP-15")).toBe(1);

  // Signed in again (the demo password form, SCR-01-AC2) and sent back to the queue: the unsent
  // note is still on the card.
  await mockApi.use({ "EP-15": "ep15-cannot-determine-4428" });
  await page.getByRole("button", { name: "Use password (demo account)" }).click();
  await page.getByLabel("Email", { exact: true }).fill("reviewer@kestrel.example");
  await page.getByLabel("Password", { exact: true }).fill("demo-password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL((url) => url.pathname === review);
  await expect(page.getByTestId("review-card").getByLabel("Note", { exact: true })).toHaveValue(note);
});
