import { expect, test } from "@playwright/test";

import {
  canCreateRun,
  canFinalise,
  canResolve,
  canRetry,
  isSignedOutPath,
  loginUrl,
  permissionsFor,
  toSessionPerson,
} from "../../lib/me";

// 006-web T013: lib/me.ts role helpers (F-12.R4, FR-003) and the sign-in path helpers (FR-001).
// The API stays the authority; these only decide which actions the screens show.

test("F-12.R4 CONTROLLER can do everything", () => {
  expect(canCreateRun("CONTROLLER")).toBe(true);
  expect(canFinalise("CONTROLLER")).toBe(true);
  expect(canResolve("CONTROLLER")).toBe(true);
  expect(canRetry("CONTROLLER")).toBe(true);
  expect(permissionsFor("CONTROLLER")).toEqual({
    createRun: true,
    finalise: true,
    resolve: true,
    retry: true,
  });
});

test("F-12.R4 REVIEWER can only resolve review items", () => {
  expect(permissionsFor("REVIEWER")).toEqual({
    createRun: false,
    finalise: false,
    resolve: true,
    retry: false,
  });
});

test("F-12.R4 OPS can only retry a failed step", () => {
  expect(permissionsFor("OPS")).toEqual({
    createRun: false,
    finalise: false,
    resolve: false,
    retry: true,
  });
});

test("F-12.R4 no role (not loaded yet) shows no actions", () => {
  for (const role of [null, undefined]) {
    expect(permissionsFor(role)).toEqual({
      createRun: false,
      finalise: false,
      resolve: false,
      retry: false,
    });
  }
});

test("FR-003 the EP-02 answer becomes the session person", () => {
  expect(
    toSessionPerson({ email: "r@kestrel.example", display_name: "Rae", role: "REVIEWER" }),
  ).toEqual({
    email: "r@kestrel.example",
    displayName: "Rae",
    role: "REVIEWER",
    can: { createRun: false, finalise: false, resolve: true, retry: false },
  });
});

test("FR-001 loginUrl carries the requested address in next", () => {
  expect(loginUrl("/runs")).toBe("/login?next=%2Fruns");
  expect(loginUrl("/runs/r-1/review?item=3")).toBe("/login?next=%2Fruns%2Fr-1%2Freview%3Fitem%3D3");
  // never bounce back to the sign-in pages themselves
  expect(loginUrl("/login?next=%2Fruns")).toBe("/login");
  expect(loginUrl("/")).toBe("/login");
});

test("FR-001 only the sign-in pages and test pages are reachable signed out", () => {
  for (const p of ["/login", "/auth/callback", "/__test__/badges", "/__test__"]) {
    expect(isSignedOutPath(p), p).toBe(true);
  }
  for (const p of ["/", "/runs", "/runs/new", "/runs/r-1/review", "/forbidden", "/loginx", "/auth"]) {
    expect(isSignedOutPath(p), p).toBe(false);
  }
});
