import { expect, test } from "@playwright/test";

// Temporary (006-web T005/T009): proves `playwright test --project=unit` runs with no browser
// and no webServer. Delete once a real unit spec exists (e.g. wording.spec.ts, T011).
test("harness sanity: 1 + 1", () => {
  expect(1 + 1).toBe(2);
});
