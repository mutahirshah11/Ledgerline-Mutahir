/**
 * Screen-test mock of the Ledgerline API and Supabase Auth (006-web T008, research R12/R13).
 *
 * Use in every `screens` spec instead of `@playwright/test`:
 *
 *   import { expect, test } from "./support/mock-api";
 *
 *   test("SCR-04-AC5 ...", async ({ page, mockApi }) => {
 *     await mockApi.signIn("CONTROLLER", "pre-review");        // session cookie + scenario
 *     await mockApi.use({ "EP-17": "ep17-summary-failed" });     // optional per-endpoint overrides
 *     await page.goto(`/runs/${RUN_IDS.main}`);
 *     ...
 *     await expect.poll(() => mockApi.count("EP-17")).toBe(1);
 *   });
 *
 * How requests are answered
 * - API calls from the browser (`/api/v1/**` on the app origin) are caught with `page.route`
 *   and forwarded to the mock server; API calls the Next server makes itself (server
 *   components, `lib/server-api.ts`) go straight to `API_BASE_URL` = the mock server (Playwright
 *   cannot route a server's own fetches). Both land in one place, so overrides, sequences and
 *   the call log are the same whoever calls. Routing rules: header of `./mock-server.ts`.
 * - The mock server is one detached Node process on the API port (3999) and the Supabase port
 *   (54321), shared by all workers and parallel runs, started on demand by the worker-scoped
 *   `mockServer` fixture, replaced when its source changes, gone after 2 idle minutes.
 * - Which test a request belongs to: the *session* id inside the fake access token
 *   (`makeAccessToken`; JWT-shaped `base64url(header).base64url(claims).sig`, header
 *   `{"alg":"HS256","typ":"JWT"}` with no `kid`, `exp` 2100-01-01, `email` of the role, and
 *   `ll: {role, scenario, session}`). Browser calls also carry `x-mock-session`.
 * - Supabase Auth: browser calls to `NEXT_PUBLIC_SUPABASE_URL/auth/v1/*` are fulfilled here
 *   (`/token` password/refresh, `/otp`, `/user`, `/logout`); the server-side `getUser()` in
 *   proxy.ts reaches the mock server's Supabase port. Password sign-in returns a session for
 *   the email's role (seeded emails; anything else is STRANGER -> EP-02 403).
 * - Signed links (`http://storage.test/...`, from the fixtures): PUT uploads answer 200;
 *   `inputs/.../p{n}-*.jpg` serves the real `t4_register_page{n}.jpg`; report links serve a tiny
 *   HTML / CSV. Logged with endpoint "STORAGE".
 *
 * What the app must do for this to work (and does in production anyway)
 * - Keep the Supabase session in @supabase/ssr cookies (`sb-127-auth-token`, `base64-` JSON);
 *   send `Authorization: Bearer <access_token>` on every API call, server- and browser-side;
 *   call the API at `/api/v1` from the browser and at `API_BASE_URL` from the server.
 * - Never verify the token's signature in the web app (the API does that; here the mock).
 *
 * Exports
 * - `test` (with fixtures `mockApi`, auto, per test; `mockServer`, auto, per worker), `expect`.
 * - `MockApi`: `session`, `scenario(name)`, `use(overrides)`, `reset()`, `signIn(role?,
 *   scenario?)`, `signOut()`, `token(role?)`, `calls(filter?)`, `count(endpoint, filter?)`,
 *   `fixture(name)`, `index`.
 * - `signIn(page, role, scenario, session?)` standalone (no `mockApi` needed; scenario from
 *   the token), `authCookie(...)`, `packFile(name)`, `fixture(name)`, `fixtureIndex()`,
 *   `RUN_IDS`, `REVIEW_ITEM_IDS`, `SOURCE_ROW_IDS`, `SCENARIOS`, `FIXTURES_DIR`, `PACK_DIR`,
 *   `API_BASE_URL`, `SUPABASE_URL`, `ensureMockServer()`, and the mock-server types.
 */
import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

import { test as base, type Page } from "@playwright/test";

import {
  DEFAULT_SCENARIO,
  fixtureStore,
  makeAccessToken,
  sourceVersion,
  supabaseReply,
  supabaseSession,
  type FixtureIndex,
  type MockCall,
  type MockServerOptions,
  type Overrides,
  type Role,
} from "./mock-server";

export { expect } from "@playwright/test";
export type {
  FixtureIndex,
  MockCall,
  Override,
  Overrides,
  Reply,
  Role,
} from "./mock-server";

// --- places and addresses ---------------------------------------------------------------

const SUPPORT_DIR = __dirname;
const SERVER_FILE = resolve(SUPPORT_DIR, "mock-server.ts");
export const FIXTURES_DIR = resolve(SUPPORT_DIR, "..", "fixtures", "pack");
export const PACK_DIR = resolve(SUPPORT_DIR, "..", "..", "..", "..", "data", "kestrel-2026-09");

/** Where the Next server sends server-side API calls (playwright.config.ts webServer env). */
export const API_BASE_URL = process.env.API_BASE_URL ?? "http://127.0.0.1:3999";
/** The Supabase URL the app is built with (playwright.config.ts webServer env). */
export const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "http://127.0.0.1:54321";
const STORAGE_ORIGIN = "http://storage.test";
const IDLE_MS = 120_000;

export type PackFileName =
  | "t4_register_page1.jpg"
  | "t4_register_page2.jpg"
  | "t4_erp_grn.csv"
  | "t4_store_sheet.xlsx";

/** Absolute path of a pack file in `data/kestrel-2026-09/` (e.g. for `setInputFiles`). */
export function packFile(name: PackFileName): string {
  return resolve(PACK_DIR, name);
}

// --- fixtures ---------------------------------------------------------------------------

const store = fixtureStore(FIXTURES_DIR);

/** The fixture index (`e2e/fixtures/pack/index.json`). */
export function fixtureIndex(): FixtureIndex {
  return store.index();
}

/** A recorded response body by fixture name (e.g. `fixture<RunView>("ep05-pre-review")`). */
export function fixture<T = unknown>(name: string): T {
  return store.load(name) as T;
}

const INDEX = store.index();
/** Run ids by label: `main` (pre-review -> finalised), `failed-extract`, `failed-proof`,
 * `duplicate`, `validation-fail`, `summary-failed`. */
export const RUN_IDS: Readonly<Record<string, string>> = INDEX.runs;
/** Review item ids of the `main` run by `item_key` (e.g. `CELL:REG-p2-r05:qty` = 4419 qty). */
export const REVIEW_ITEM_IDS: Readonly<Record<string, string>> = INDEX.review_items;
/** Source row ids by label (`4416-sheet`: the sheet row whose date was text). */
export const SOURCE_ROW_IDS: Readonly<Record<string, string>> = INDEX.source_rows;
/** Scenario names (keys of `index.json` `scenarios`). */
export const SCENARIOS: readonly string[] = Object.keys(INDEX.scenarios);

// --- auth cookie ------------------------------------------------------------------------

/** @supabase/ssr's cookie name: supabase-js `sb-<first label of the URL host>-auth-token`. */
export function authCookieName(supabaseUrl: string = SUPABASE_URL): string {
  return `sb-${new URL(supabaseUrl).hostname.split(".")[0]}-auth-token`;
}

/** The SSR auth cookie for a signed-in role: `base64-` + base64url(session JSON), one chunk. */
export function authCookie(
  role: Role,
  scenario: string,
  session: string,
  supabaseUrl: string = SUPABASE_URL,
): { name: string; value: string } {
  const json = JSON.stringify(supabaseSession(role, scenario, session, supabaseUrl));
  const value = `base64-${Buffer.from(json, "utf8").toString("base64url")}`;
  if (value.length > 3180) throw new Error("auth cookie would need chunks; keep the session small");
  return { name: authCookieName(supabaseUrl), value };
}

function newSession(): string {
  return randomUUID().replace(/-/g, "").slice(0, 16);
}

/**
 * Signs `page`'s context in as `role` by setting the auth cookie for the app origin. The
 * scenario travels in the token, so this works without the `mockApi` fixture's session
 * config (overrides then need `mockApi`). Call before `page.goto`.
 */
export async function signIn(
  page: Page,
  role: Role,
  scenario: string = DEFAULT_SCENARIO,
  session: string = newSession(),
  baseURL: string = `http://localhost:${process.env.PW_PORT ?? "3100"}`,
): Promise<void> {
  const { name, value } = authCookie(role, scenario, session);
  await page.context().addCookies([{ name, value, url: baseURL, sameSite: "Lax" }]);
}

// --- the shared mock server -------------------------------------------------------------

type Health = { ok: boolean; version: string };

async function probe(base: string): Promise<Health | null> {
  try {
    const res = await fetch(`${base}/__mock/health`, { signal: AbortSignal.timeout(1000) });
    return res.ok ? ((await res.json()) as Health) : null;
  } catch {
    return null;
  }
}

function portOf(url: string): number {
  const u = new URL(url);
  return Number(u.port || (u.protocol === "https:" ? 443 : 80));
}

/** Starts the shared mock server unless one of this source version answers already. */
export async function ensureMockServer(): Promise<void> {
  const version = sourceVersion(readFileSync(SERVER_FILE, "utf8"));
  const running = await probe(API_BASE_URL);
  if (running?.version === version && (await probe(SUPABASE_URL))?.version === version) return;
  if (running) {
    await fetch(`${API_BASE_URL}/__mock/shutdown`, { method: "POST" }).catch(() => undefined);
    for (let i = 0; i < 50 && (await probe(API_BASE_URL)); i++) await sleep(100);
  }
  const opts: MockServerOptions = {
    fixturesDir: FIXTURES_DIR,
    apiPort: portOf(API_BASE_URL),
    supabasePort: portOf(SUPABASE_URL),
    supabaseUrl: SUPABASE_URL,
    version,
    idleMs: IDLE_MS,
  };
  const script = `const m = await import(${JSON.stringify(pathToFileURL(SERVER_FILE).href)});
await m.startMockServer(${JSON.stringify(opts)});`;
  const child = spawn(
    process.execPath,
    ["--disable-warning=MODULE_TYPELESS_PACKAGE_JSON", "--input-type=module", "-e", script],
    { detached: true, stdio: "ignore", windowsHide: true },
  );
  child.unref();
  for (let i = 0; i < 150; i++) {
    const up = await probe(API_BASE_URL);
    if (up?.version === version && (await probe(SUPABASE_URL))?.version === version) return;
    await sleep(100);
  }
  throw new Error(
    `mock server did not start on ${API_BASE_URL} and ${SUPABASE_URL} ` +
      "(is another program, e.g. a local Supabase, using those ports?)",
  );
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// --- the per-test fixture ---------------------------------------------------------------

export type CallFilter = { method?: string; via?: "browser" | "server"; path?: string | RegExp };

export type MockApi = {
  /** This test's session id (in its tokens). */
  readonly session: string;
  /** The fixture index. */
  readonly index: FixtureIndex;
  /** Selects the scenario (default `pre-review`); keeps the overrides. */
  scenario(name: string): Promise<void>;
  /** Adds per-endpoint overrides (keys `"EP-17"` or `"POST /runs/{id}/finalise"`); merges. */
  use(overrides: Overrides): Promise<void>;
  /** Back to `pre-review`, no overrides; sequences restart. */
  reset(): Promise<void>;
  /** Sets the auth cookie for `role` (default CONTROLLER); `scenario` also selects it. */
  signIn(role?: Role, scenario?: string): Promise<void>;
  /** Clears the auth cookie. */
  signOut(): Promise<void>;
  /** A bearer token of this session for `role` (default CONTROLLER). */
  token(role?: Role): string;
  /** Every call of this test, oldest first: API (server log) plus storage and auth calls. */
  calls(filter?: CallFilter & { endpoint?: string }): Promise<MockCall[]>;
  /** How many calls hit `endpoint` ("EP-17", "STORAGE", "AUTH"). */
  count(endpoint: string, filter?: CallFilter): Promise<number>;
  /** A recorded body by name. */
  fixture<T = unknown>(name: string): T;
};

function matches(call: MockCall, f: CallFilter & { endpoint?: string }): boolean {
  if (f.endpoint && call.endpoint !== f.endpoint) return false;
  if (f.method && call.method !== f.method.toUpperCase()) return false;
  if (f.via && call.via !== f.via) return false;
  if (f.path !== undefined) {
    const ok = typeof f.path === "string" ? call.path === f.path : f.path.test(call.path);
    if (!ok) return false;
  }
  return true;
}

/** Orders calls logged here and in the mock server (same clock, same formula). */
function callSeq(): number {
  return Date.now() * 1000 + (process.hrtime()[1] % 1000);
}

const REPORT_BODIES: Record<string, { type: string; body: string }> = {
  html: { type: "text/html", body: "<!doctype html><title>Report</title><h1>Report</h1>" },
  csv: { type: "text/csv", body: "grn_no,outcome\n4419,MISMATCHED\n" },
};

export const test = base.extend<{ mockApi: MockApi }, { mockServer: void }>({
  mockServer: [
    async ({}, use) => {
      await ensureMockServer();
      await use();
    },
    { scope: "worker", auto: true },
  ],

  mockApi: [
    async ({ page, baseURL }, use) => {
      const session = newSession();
      const sessionUrl = `${API_BASE_URL}/__mock/sessions/${session}`;
      let config: { scenario: string; overrides: Overrides } = {
        scenario: DEFAULT_SCENARIO,
        overrides: {},
      };
      const push = async () => {
        const res = await fetch(sessionUrl, {
          method: "PUT",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(config),
        });
        if (!res.ok) throw new Error(`mock-api: ${res.status} ${await res.text()}`);
      };
      await push();
      const local: MockCall[] = [];
      const log = (call: Omit<MockCall, "seq" | "via">) =>
        local.push({ ...call, via: "browser", seq: callSeq() });

      await page.route("**/api/v1/**", async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        try {
          const response = await route.fetch({
            url: `${API_BASE_URL}${url.pathname}${url.search}`,
            headers: { ...req.headers(), "x-mock-via": "browser", "x-mock-session": session },
          });
          await route.fulfill({ response });
        } catch (err) {
          // A delayed reply still in flight when the test ends: the page is gone, nothing to answer.
          if (page.isClosed() || /Test ended|Target .*closed|has been closed|has been disposed/i.test(String(err))) return;
          throw err;
        }
      });

      await page.route(`${SUPABASE_URL}/auth/v1/**`, async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        const cors = {
          "access-control-allow-origin": req.headers()["origin"] ?? "*",
          "access-control-allow-credentials": "true",
          "access-control-allow-headers": "*",
          "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS",
        };
        if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
        let body: unknown = null;
        try {
          body = req.postDataJSON();
        } catch {
          body = req.postData();
        }
        const query = Object.fromEntries(url.searchParams.entries());
        const auth = await req.headerValue("authorization");
        const defaults = { scenario: config.scenario, session };
        const r = supabaseReply(req.method(), url.pathname, query, body, auth ?? undefined, SUPABASE_URL, defaults);
        log({ method: req.method(), path: url.pathname, query, body, endpoint: "AUTH", fixture: null, status: r.status });
        if (r.status === 204) return route.fulfill({ status: 204, headers: cors });
        await route.fulfill({ status: r.status, headers: cors, json: r.body });
      });

      await page.route(`${STORAGE_ORIGIN}/**`, async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        const query = Object.fromEntries(url.searchParams.entries());
        const cors = { "access-control-allow-origin": "*", "access-control-allow-headers": "*" };
        const entry = { method: req.method(), path: url.pathname, query, body: null, endpoint: "STORAGE", fixture: null };
        if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: cors });
        if (req.method() === "PUT" || req.method() === "POST") {
          log({ ...entry, status: 200 });
          return route.fulfill({ status: 200, headers: cors, json: { Key: url.pathname.slice(1) } });
        }
        const page = url.pathname.match(/\/p(\d+)-[0-9a-f]+\.(jpe?g|png)$/);
        if (page) {
          log({ ...entry, status: 200 });
          const file = packFile(`t4_register_page${page[1]}.jpg` as PackFileName);
          return route.fulfill({ status: 200, headers: cors, contentType: "image/jpeg", body: readFileSync(file) });
        }
        const report = url.pathname.endsWith(".csv") ? REPORT_BODIES.csv : url.pathname.endsWith(".html") ? REPORT_BODIES.html : null;
        log({ ...entry, status: report ? 200 : 404 });
        if (!report) return route.fulfill({ status: 404, headers: cors, body: "not found" });
        await route.fulfill({ status: 200, headers: cors, contentType: report.type, body: report.body });
      });

      const appUrl = baseURL ?? `http://localhost:${process.env.PW_PORT ?? "3100"}`;
      const api: MockApi = {
        session,
        index: fixtureIndex(),
        async scenario(name) {
          config = { ...config, scenario: name };
          await push();
        },
        async use(overrides) {
          config = { ...config, overrides: { ...config.overrides, ...overrides } };
          await push();
        },
        async reset() {
          config = { scenario: DEFAULT_SCENARIO, overrides: {} };
          await push();
        },
        async signIn(role = "CONTROLLER", scenario) {
          if (scenario !== undefined) await api.scenario(scenario);
          await signIn(page, role, config.scenario, session, appUrl);
        },
        async signOut() {
          await page.context().clearCookies({ name: authCookieName() });
        },
        token(role = "CONTROLLER") {
          return makeAccessToken(role, config.scenario, session, SUPABASE_URL);
        },
        async calls(filter = {}) {
          const res = await fetch(`${sessionUrl}/calls`);
          const server = (await res.json()) as MockCall[];
          const all = [...server, ...local].sort((a, b) => a.seq - b.seq);
          return all.filter((c) => matches(c, filter));
        },
        async count(endpoint, filter = {}) {
          return (await api.calls({ ...filter, endpoint })).length;
        },
        fixture,
      };
      await use(api);
      await fetch(sessionUrl, { method: "DELETE" }).catch(() => undefined);
    },
    { auto: true },
  ],
});
