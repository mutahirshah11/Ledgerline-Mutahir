// 006-web (US1/US6 shell checks): signed-out gate (proxy.ts) and the signed-in app shell
// (skip link + <main id="main">, layout.tsx).

import { expect, test } from "./support/mock-api";

test("shell: signed-out / redirects to /login?next=%2Fruns", async ({ page, mockApi }) => {
  await mockApi.signOut();
  // "/" itself redirects to /runs (lib/me.ts safeNext's fallback), so proxy.ts's own `next`
  // (lib/me.ts loginUrl: no `next` param when it would just equal the fallback) drops to plain
  // `/login` for that one path; a deeper address keeps its `next` (covered by SCR-01-AC4 already).
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);

  await page.goto("/runs");
  await expect(page).toHaveURL(/\/login\?next=%2Fruns/);
});

test("shell: signed-in CONTROLLER sees skip link and main", async ({ page, mockApi }) => {
  await mockApi.signIn("CONTROLLER", "pre-review");
  await page.goto("/runs");

  await expect(page.getByRole("link", { name: "Skip to main content" })).toBeAttached();
  await expect(page.locator("main#main")).toBeVisible();
});
