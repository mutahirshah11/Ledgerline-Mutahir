import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

// 006-web T065: WEB-AC2 smoke test (spec.md US8; FR-040; quickstart.md step 6). Playwright
// project "smoke" (playwright.config.ts): no webServer, `baseURL` comes from `BASE_URL`, and this
// file is the only one that project runs (`testMatch: "smoke.spec.ts"`). It is never run by the
// "screens" project and never run from an agent (common rules) — it hits a real deployed web app,
// a real deployed API, and a real Supabase project, driven only by:
//   BASE_URL       the deployed web app's origin (e.g. a Vercel Preview URL)
//   SMOKE_EMAIL     a seeded demo-password Supabase Auth user (CONTROLLER role; T067)
//   SMOKE_PASSWORD  that user's password
//
// Deliberately independent of e2e/support/mock-api.ts (that module exists to drive the mocked
// "screens" project): no mock server, no fixtures, no route interception. The pack files it
// uploads are the same `data/kestrel-2026-09/` set the mocked SCR-03/SCR-06 tests are recorded
// from (see e2e/support/mock-api.ts PACK_DIR), so the reason line and counts below match
// quickstart.md step 6 and e2e/scr-06-review.spec.ts's REASON_4419 — but this run is real: nothing
// here is faked, and the asserted text would also be right for a differently-seeded pack as long
// as it still produces an open GRN 4419 "Accept read" item.
//
// Everything after Run (upload -> validate -> extract each page -> reconcile) happens
// automatically inside the app once "Run" is clicked (see app/runs/new/NewRun.tsx `runFrom`); the
// screen itself navigates to the overview once reconcile finishes. Against real infra each of
// those steps is a real OCR/reconciliation call, so this test's timeout is generous (10 minutes)
// rather than the "screens" project's default.

const PACK_DIR = resolve(__dirname, "..", "..", "..", "data", "kestrel-2026-09");
const packFile = (name: string) => resolve(PACK_DIR, name);

const SMOKE_EMAIL = process.env.SMOKE_EMAIL;
const SMOKE_PASSWORD = process.env.SMOKE_PASSWORD;

test.skip(
  !process.env.BASE_URL || !SMOKE_EMAIL || !SMOKE_PASSWORD,
  "BASE_URL / SMOKE_EMAIL / SMOKE_PASSWORD not set (see specs/006-web/quickstart.md step 6)",
);

test("WEB-AC2 sign in, start a run on the pack, review GRN 4419, finalise and send", async ({ page }) => {
  // Real OCR/reconciliation/proof calls, not a mocked instant response (quickstart.md step 6).
  test.setTimeout(10 * 60 * 1000);

  // 1. Password sign-in (app/login/page.tsx; email+password only, no magic link, no sign-up). A
  // fresh sign-in lands on the FALLBACK_NEXT route, "/runs" (lib/me.ts).
  await page.goto("/login");
  await page.getByLabel("Email", { exact: true }).fill(SMOKE_EMAIL!);
  await page.getByLabel("Password", { exact: true }).fill(SMOKE_PASSWORD!);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page).toHaveURL(/\/runs$/, { timeout: 30_000 });

  // 2. New run: upload the four pack files (app/runs/new/NewRun.tsx; FileDropZone's file inputs
  // are reached by name, matching e2e/scr-03-new-run.spec.ts's convention). The month field is
  // left at its EP-01 default (quickstart.md's pack is for that default month).
  await page.getByRole("link", { name: "New run" }).click();
  await expect(page).toHaveURL(/\/runs\/new$/);
  await expect(page.getByLabel("Month")).not.toHaveValue("");

  await page
    .locator('input[type="file"][name="register"]')
    .setInputFiles([packFile("t4_register_page1.jpg"), packFile("t4_register_page2.jpg")]);
  await page.locator('input[type="file"][name="erp"]').setInputFiles(packFile("t4_erp_grn.csv"));
  await page.locator('input[type="file"][name="sheet"]').setInputFiles(packFile("t4_store_sheet.xlsx"));
  await page.getByRole("button", { name: "Run", exact: true }).click();

  // 3. The app drives upload -> validate -> extract -> reconcile itself and then navigates to the
  // overview; wait for that navigation, then for the headline (run.headline, RunOverview.tsx h1)
  // to actually have text (it starts empty only for an instant, but a real reconcile is slow, so
  // this is the generous wait).
  await expect(page).toHaveURL(/\/runs\/[^/]+$/, { timeout: 9 * 60 * 1000 });
  const runUrl = page.url();
  const headline = page.getByRole("heading", { level: 1 });
  await expect(headline).not.toHaveText("", { timeout: 5 * 60 * 1000 });

  // 4. Review queue: GRN 4419 is the first open item for this pack (e2e/scr-06-review.spec.ts
  // REASON_4419, recorded from the same pack) and is a corrected quantity both reads agree on, so
  // "Accept read" is enabled without entering a value. Navigated to directly rather than via the
  // overview's "Review now" link, which only shows while review is pending.
  await page.goto(`${runUrl}/review`);
  const card = page.getByTestId("review-card");
  await expect(card.getByTestId("reason-line")).toContainText("4419");
  const acceptRead = card.getByRole("button", { name: "Accept read", exact: true });
  await expect(acceptRead).toBeEnabled();
  await acceptRead.click();

  // 5. Back to the run overview (the queue's own "Run overview" link is always present, unlike
  // the empty-queue-only "Back to overview" link).
  await page.getByRole("link", { name: "Run overview" }).click();
  await expect(page).toHaveURL(runUrl);

  // 6. Finalise and send: the trigger button opens a confirm dialog whose own button has the same
  // accessible name (components/FinalisePanel.tsx hides the trigger while the dialog is open, so
  // this is never a strict-mode ambiguity), then assert the sent banner's exact wording prefix.
  const finaliseButton = page.getByRole("button", { name: "Finalise and send", exact: true });
  await finaliseButton.click();
  await finaliseButton.click();
  await expect(page.getByRole("status")).toContainText("Finalised. Summary sent", { timeout: 60_000 });
});
