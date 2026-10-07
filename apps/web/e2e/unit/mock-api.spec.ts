import { existsSync } from "node:fs";

import { expect, test } from "@playwright/test";

import {
  authCookie,
  fixture,
  fixtureIndex,
  packFile,
  RUN_IDS,
  REVIEW_ITEM_IDS,
} from "../support/mock-api";
import {
  findEndpoint,
  makeAccessToken,
  parseAccessToken,
  parseRefreshToken,
  pickFixture,
  resolveReply,
  supabaseReply,
  type FixtureIndex,
  type MockRequest,
  type SessionConfig,
} from "../support/mock-server";

// 006-web T008: the pure parts of the screen-test mock (e2e/support/mock-api.ts and
// mock-server.ts) over the recorded pack fixtures (T007). No browser, no server.

const SUPABASE = "http://127.0.0.1:54321";
const index: FixtureIndex = fixtureIndex();
const main = RUN_IDS.main;
const token = (role: "CONTROLLER" | "REVIEWER" | "OPS" | "STRANGER" = "CONTROLLER") =>
  parseAccessToken(makeAccessToken(role, "pre-review", "s-1", SUPABASE));

function reply(
  config: SessionConfig,
  req: Partial<MockRequest> & { path: string },
  counters = new Map<string, number>(),
) {
  const request: MockRequest = { method: "GET", query: {}, token: token(), ...req };
  return resolveReply(index, config, counters, request, fixture);
}

const preReview: SessionConfig = { scenario: "pre-review", overrides: {} };

test("token is JWT-shaped, unsigned HS256 without kid, and round-trips role, scenario, session", () => {
  const raw = makeAccessToken("REVIEWER", "post-review", "abc", SUPABASE);
  const header = JSON.parse(Buffer.from(raw.split(".")[0], "base64url").toString("utf8"));
  expect(raw.split(".")).toHaveLength(3);
  expect(header).toEqual({ alg: "HS256", typ: "JWT" });
  const claims = parseAccessToken(`Bearer ${raw}`);
  expect(claims?.email).toBe("reviewer@kestrel.example");
  expect(claims?.ll).toEqual({ role: "REVIEWER", scenario: "post-review", session: "abc" });
  expect(claims?.iss).toBe(`${SUPABASE}/auth/v1`);
  expect(claims?.exp).toBeGreaterThan(Date.UTC(2099, 0, 1) / 1000);
  expect(parseAccessToken("not-a-token")).toBeNull();
  expect(parseRefreshToken("mock-refresh.OPS.finalised.s-9")).toEqual({
    role: "OPS",
    scenario: "finalised",
    session: "s-9",
  });
});

test("auth cookie is @supabase/ssr's sb-<host label>-auth-token, base64- prefixed session JSON", () => {
  const cookie = authCookie("CONTROLLER", "finalised", "s-2", SUPABASE);
  expect(cookie.name).toBe("sb-127-auth-token");
  expect(cookie.value.startsWith("base64-")).toBe(true);
  const session = JSON.parse(Buffer.from(cookie.value.slice(7), "base64url").toString("utf8"));
  expect(session.token_type).toBe("bearer");
  expect(parseAccessToken(session.access_token)?.ll).toEqual({
    role: "CONTROLLER",
    scenario: "finalised",
    session: "s-2",
  });
  expect(session.user.email).toBe("controller@kestrel.example");
  expect(cookie.value.length).toBeLessThan(3180); // one cookie, no chunks
});

test("endpoints are found by method and path template", () => {
  expect(findEndpoint(index, "GET", `/runs/${main}`)?.endpoint).toBe("EP-05");
  expect(findEndpoint(index, "post", `/runs/${main}/finalise`)?.key).toBe(
    "POST /runs/{id}/finalise",
  );
  expect(findEndpoint(index, "GET", `/runs/${main}/nope`)).toBeNull();
});

test("scenario fixture: query params must match, the best path match wins, run ids need not", () => {
  const groups = `/runs/${main}/groups`;
  expect(pickFixture(index, "pre-review", "GET", groups, {})).toBe("ep11-groups");
  expect(pickFixture(index, "pre-review", "GET", groups, { outcome: "MISMATCHED" })).toBe(
    "ep11-groups-mismatched",
  );
  const otherRun = "00000000-0000-4000-8000-0000000000ff";
  expect(pickFixture(index, "pre-review", "GET", `/runs/${otherRun}/pages/2/image`, {})).toBe(
    "ep16-page-2",
  );
  const item4428 = REVIEW_ITEM_IDS["CELL:REG-p2-r14:qty"];
  expect(pickFixture(index, "pre-review", "POST", `/review-items/${item4428}/resolve`, {})).toBe(
    "ep15-enter-value-4428",
  );
  expect(pickFixture(index, "finalised", "GET", `/runs/${main}`, {})).toBe("ep05-finalised");
  expect(() => pickFixture(index, "no-such-scenario", "GET", "/runs", {})).toThrow(/scenario/);
});

test("every scenario names recorded fixtures and answers EP-01, EP-02 and EP-04", () => {
  for (const [name, fixtures] of Object.entries(index.scenarios)) {
    for (const f of fixtures) expect(index.fixtures[f], `${name}: ${f}`).toBeTruthy();
    const runs = reply({ scenario: name, overrides: {} }, { path: "/runs" });
    expect(runs.status, name).toBe(200);
  }
  expect(reply({ scenario: "empty", overrides: {} }, { path: "/runs" }).body).toEqual([]);
});

test("no token is 401 except EP-01; EP-02 answers the token's role, STRANGER 403", () => {
  expect(reply(preReview, { path: "/me", token: null }).status).toBe(401);
  expect(reply(preReview, { path: "/health", token: null }).status).toBe(200);
  expect(reply(preReview, { path: "/me", token: token("OPS") }).body).toMatchObject({
    role: "OPS",
  });
  const stranger = reply(preReview, { path: "/me", token: token("STRANGER") });
  expect(stranger.status).toBe(403);
  expect(stranger.body).toMatchObject({ error: { code: "NOT_ALLOWED" } });
});

test("overrides by EP id or METHOD /template; a sequence advances and repeats its last", () => {
  const counters = new Map<string, number>();
  const config: SessionConfig = {
    scenario: "pre-review",
    overrides: {
      "EP-14": ["ep14-open-pre-review", "ep14-open-after-1", { status: 200, body: [] }],
      "POST /runs/{id}/finalise": { fixture: "ep17-summary-failed", delayMs: 5 },
      "EP-01": { status: 503, body: { status: "degraded" } },
    },
  };
  const items = () =>
    (reply(config, { path: `/runs/${main}/review-items`, query: { status: "OPEN" } }, counters)
      .body as unknown[]).length;
  expect([items(), items(), items(), items()]).toEqual([4, 3, 0, 0]);
  const fin = reply(config, { method: "POST", path: `/runs/${main}/finalise` }, counters);
  expect(fin).toMatchObject({ status: 200, fixture: "ep17-summary-failed", delayMs: 5 });
  expect(reply(config, { path: "/health", token: null }, counters).status).toBe(503);
});

test("unknown routes are 404 MOCK_NO_ROUTE; a scenario without the endpoint is 501", () => {
  expect(reply(preReview, { path: "/nope" })).toMatchObject({ status: 404 });
  const missing = reply({ scenario: "empty", overrides: {} }, { path: `/runs/${main}/proof` });
  expect(missing.status).toBe(501);
  expect(missing.body).toMatchObject({ error: { code: "MOCK_NO_FIXTURE" } });
});

test("Supabase auth: password grant signs the email's role in; user, otp and logout answer", () => {
  const defaults = { scenario: "finalised", session: "s-3" };
  const signIn = supabaseReply(
    "POST",
    "/auth/v1/token",
    { grant_type: "password" },
    { email: "Reviewer@kestrel.example", password: "x" },
    undefined,
    SUPABASE,
    defaults,
  );
  const session = signIn.body as { access_token: string };
  expect(parseAccessToken(session.access_token)?.ll).toEqual({
    role: "REVIEWER",
    scenario: "finalised",
    session: "s-3",
  });
  const user = supabaseReply("GET", "/auth/v1/user", {}, null, `Bearer ${session.access_token}`, SUPABASE, defaults);
  expect(user).toMatchObject({ status: 200, body: { email: "reviewer@kestrel.example" } });
  expect(supabaseReply("GET", "/auth/v1/user", {}, null, undefined, SUPABASE, defaults).status).toBe(401);
  expect(supabaseReply("POST", "/auth/v1/otp", {}, {}, undefined, SUPABASE, defaults).status).toBe(200);
  expect(supabaseReply("POST", "/auth/v1/logout", {}, null, undefined, SUPABASE, defaults).status).toBe(204);
});

test("pack files resolve to data/kestrel-2026-09 and fixtures carry the real pack numbers", () => {
  for (const name of [
    "t4_register_page1.jpg",
    "t4_register_page2.jpg",
    "t4_erp_grn.csv",
    "t4_store_sheet.xlsx",
  ] as const) {
    expect(existsSync(packFile(name)), name).toBe(true);
  }
  const run = fixture<{ counts: Record<string, number>; status: string }>("ep05-pre-review");
  expect(run.status).toBe("AWAITING_REVIEW");
  expect(run.counts).toMatchObject({ MATCHED: 21, MISMATCHED: 4, MISSING: 2, NEEDS_REVIEW: 2 });
  expect(Object.keys(RUN_IDS)).toEqual(
    expect.arrayContaining(["main", "failed-extract", "failed-proof", "summary-failed"]),
  );
});
