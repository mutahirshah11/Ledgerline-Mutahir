// 006-web T025 revision (US1): SCR-01 sign-in (spec.md SCR-01-AC1..AC4, FR-001..FR-003).
// Email + password only — no magic link.
//
// Uses the `screens` mockApi fixture (apps/web/e2e/support/mock-api.ts): Supabase Auth calls are
// answered in-worker (any password "succeeds"; the email decides the role, STRANGER for anything
// not seeded) and EP-02 answers the role per the default "pre-review" scenario.

import { expect, test } from "./support/mock-api";

test("SCR-01-AC1 entering an email and password and pressing Sign in signs in, no magic link or sign-up offered", async ({
  page,
  mockApi,
}) => {
  await mockApi.reset();
  await page.goto("/login");

  await expect(page.getByRole("button", { name: "Send link" })).toHaveCount(0);
  await expect(page.getByText(/sign.?up/i)).toHaveCount(0);

  await page.getByLabel("Email").fill("controller@kestrel.example");
  await page.getByLabel("Password").fill("demo-password");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/runs$/);
});

test("SCR-01-AC2 a demo account signs in with email and password", async ({ page, mockApi }) => {
  await mockApi.reset();
  await page.goto("/login");

  await page.getByLabel("Email").fill("controller@kestrel.example");
  await page.getByLabel("Password").fill("demo-password");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/runs$/);
  // Signed in as CONTROLLER (EP-02 pre-review scenario): "New run" is on the runs list.
  await expect(page.getByRole("link", { name: "New run" })).toBeVisible();
});

test("SCR-01-AC3 a signed-in person off the allowlist sees the 403 page and no run data is requested", async ({
  page,
  mockApi,
}) => {
  await mockApi.reset();
  await page.goto("/login");

  await page.getByLabel("Email").fill("nobody@example.com");
  await page.getByLabel("Password").fill("whatever");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/forbidden$/);
  await expect(page.getByText("Your email is not on the list for this app")).toBeVisible();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await expect.poll(() => mockApi.count("EP-04")).toBe(0);
});

test("SCR-01-AC4 a signed-out visitor opening a /runs address is sent to /login and back after sign-in", async ({
  page,
  mockApi,
}) => {
  await mockApi.reset();
  await page.goto("/runs");

  await expect(page).toHaveURL(/\/login\?next=%2Fruns/);

  await page.getByLabel("Email").fill("controller@kestrel.example");
  await page.getByLabel("Password").fill("demo-password");
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL(/\/runs$/);
});
