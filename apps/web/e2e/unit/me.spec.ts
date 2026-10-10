import { expect, test } from "@playwright/test";

import {
  canCreateRun,
  canFinalise,
  canResolve,
  canRetry,
  isSignedOutPath,
  loginUrl,
  permissionsFor,
  safeNext,
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
  // never an off-site or protocol-relative target
  expect(loginUrl("//evil.example/x")).toBe("/login");
  expect(loginUrl("/\\evil.example")).toBe("/login");
});

test("FR-001 loginUrl marks an expired session so /login can sign out locally first", () => {
  expect(loginUrl("/runs/r-1", { expired: true })).toBe("/login?next=%2Fruns%2Fr-1&expired=1");
  expect(loginUrl("/", { expired: true })).toBe("/login?expired=1");
});

test("FR-001 safeNext keeps in-app paths and falls back to /runs for anything else", () => {
  expect(safeNext("/runs/r-1/review?item=3")).toBe("/runs/r-1/review?item=3");
  expect(safeNext("/runs")).toBe("/runs");
  expect(safeNext("/forbidden")).toBe("/forbidden");
  for (const bad of [
    null,
    undefined,
    "",
    "/",
    "runs",
    "//evil.example",
    "//evil.example/runs",
    "/\\evil.example",
    "/runs\\x",
    "https://evil.example/runs",
    "javascript:alert(1)",
    "/runs\nSet-Cookie:x",
    "/runs\u0000",
    "/runs\u007f",
    "/login",
    "/login?next=%2Fruns",
    "/auth",
    "/auth/callback?code=1",
  ]) {
    expect(safeNext(bad), String(bad)).toBe("/runs");
  }
});

test("FR-001 only the sign-in pages, the public landing and test pages are reachable signed out", () => {
  // "/" is the public marketing landing (app/page.tsx) — it redirects a signed-in visitor to
  // /runs itself, so it's safe to let a signed-out visitor land there too.
  for (const p of ["/", "/login", "/auth/callback", "/__test__/badges", "/__test__"]) {
    expect(isSignedOutPath(p), p).toBe(true);
  }
  for (const p of ["/runs", "/runs/new", "/runs/r-1/review", "/forbidden", "/loginx", "/auth"]) {
    expect(isSignedOutPath(p), p).toBe(false);
  }
});
