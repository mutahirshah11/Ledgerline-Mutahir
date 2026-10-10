// 006-web (US1/US6 shell checks): signed-out gate (proxy.ts) and the signed-in app shell
// (skip link + <main id="main">, layout.tsx).

import { expect, test } from "./support/mock-api";

test("shell: signed-out / shows the public landing page, not a redirect", async ({ page, mockApi }) => {
  await mockApi.signOut();
  // "/" is now a public marketing landing (app/page.tsx, lib/me.ts SIGNED_OUT_PREFIXES) — a
  // signed-out visitor stays there and signs in via its own "Sign in" link, rather than being
  // bounced straight to /login. A deeper address still redirects with its `next` (SCR-01-AC4).
  await page.goto("/");
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in", exact: true })).toBeVisible();

  await page.goto("/runs");
  await expect(page).toHaveURL(/\/login\?next=%2Fruns/);
});

test("shell: signed-in CONTROLLER sees skip link and main", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto("/runs");

  await expect(page.getByRole("link", { name: "Skip to main content" })).toBeAttached();
  await expect(page.locator("main#main")).toBeVisible();
});
