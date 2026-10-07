/**
 * Mock Ledgerline API + Supabase Auth for the `screens` project (006-web T008, research R12/R13).
 *
 * Self-contained on purpose (only `node:` imports, erasable TypeScript only): Playwright workers
 * import it (pure routing, unit-tested in e2e/unit/mock-api.spec.ts), and `mock-api.ts` also
 * runs it as a detached Node process (`node` strips the types) that listens on
 *   - the API port (`API_BASE_URL`, default 127.0.0.1:3999): `/api/v1/**`, served from the
 *     recorded pack fixtures (`e2e/fixtures/pack/`, apps/api/scripts/record_web_fixtures.py), and
 *   - the Supabase port (`NEXT_PUBLIC_SUPABASE_URL`, default 127.0.0.1:54321): `/auth/v1/*`.
 * One process serves every worker and every parallel Playwright run on this machine: all state
 * (scenario, overrides, recorded calls) is kept per *session*, a random id carried in the fake
 * access token (see `makeAccessToken`), so tests never see each other. It exits after
 * `idleMs` without a request. Fixture files are re-read when they change on disk.
 *
 * Request -> response (`resolveReply`):
 *   1. the endpoint is found from the fixture index (method + path template, e.g.
 *      `GET /runs/{id}`); unknown -> 404 `MOCK_NO_ROUTE`;
 *   2. no bearer token on anything but EP-01 -> 401 (`ep02-unauthenticated`), as the API does;
 *   3. a per-session override for the endpoint (by EP id or `METHOD /template`) wins; an array
 *      override is served in order, its last entry repeating;
 *   4. EP-02 `/me` answers the token's role (`ep02-me-<role>`, STRANGER -> 403);
 *   5. otherwise the scenario's fixtures: same endpoint, every recorded query param present in
 *      the request with the same value; the best match has the most equal path segments (so a
 *      page number or a review item id picks its own fixture, a run id never has to match),
 *      then the most query params; ties go to the earlier name in the scenario list;
 *   6. nothing -> 501 `MOCK_NO_FIXTURE` (the test fails loudly, naming scenario and endpoint).
 */
import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import type { IncomingMessage, Server, ServerResponse } from "node:http";
import { join } from "node:path";

// --- fixture index (written by record_web_fixtures.py) ----------------------------------

export type FixtureMeta = {
  endpoint: string;
  method: string;
  template: string;
  path: string;
  query: Record<string, string>;
  status: number;
  role: string | null;
  file: string;
};

export type FixtureIndex = {
  fixtures: Record<string, FixtureMeta>;
  runs: Record<string, string>;
  review_items: Record<string, string>;
  source_rows: Record<string, string>;
  scenarios: Record<string, string[]>;
};

export type Role = "CONTROLLER" | "REVIEWER" | "OPS" | "STRANGER";

export const ROLES: readonly Role[] = ["CONTROLLER", "REVIEWER", "OPS", "STRANGER"];

export const EMAILS: Record<Role, string> = {
  CONTROLLER: "controller@kestrel.example",
  REVIEWER: "reviewer@kestrel.example",
  OPS: "ops@kestrel.example",
  STRANGER: "stranger@kestrel.example",
};

export const DEFAULT_SCENARIO = "pre-review";
export const API_PREFIX = "/api/v1";

/** A reply: a fixture name, or an explicit status/body (or fixture) with an optional delay. */
export type Reply =
  | string
  | { status?: number; body?: unknown; fixture?: string; delayMs?: number };
/** Per-endpoint override: one reply, or a sequence (served in order; the last repeats). */
export type Override = Reply | Reply[];
/** Keys: an EP id (`"EP-17"`) or `"METHOD /template"` (`"POST /runs/{id}/finalise"`). */
export type Overrides = Record<string, Override>;

export type SessionConfig = { scenario: string; overrides: Overrides };

export type MockRequest = {
  method: string;
  /** Path below `/api/v1`, e.g. `/runs/<id>/groups`. */
  path: string;
  query: Record<string, string>;
  token: TokenClaims | null;
};

export type ResolvedReply = {
  status: number;
  body: unknown;
  fixture: string | null;
  endpoint: string | null;
  delayMs: number;
};

export type MockCall = {
  method: string;
  /** Path below `/api/v1` for API calls; the full URL path for storage/auth calls. */
  path: string;
  query: Record<string, string>;
  body: unknown;
  /** "EP-05" etc.; "STORAGE" for signed-link PUT/GET; "AUTH" for Supabase Auth. */
  endpoint: string | null;
  fixture: string | null;
  status: number;
  /** "browser": routed from the page (`page.route`); "server": the Next server called it. */
  via: "browser" | "server";
  /** Order key: milliseconds x 1000 + a counter (calls of one test sort chronologically). */
  seq: number;
};

// --- tokens -----------------------------------------------------------------------------

/** The claims mock-api puts in its fake access token (`ll` is ours; the rest is Supabase's). */
export type TokenClaims = {
  sub: string;
  email: string;
  aud: "authenticated";
  role: "authenticated";
  exp: number;
  iat: number;
  iss: string;
  session_id: string;
  app_metadata: { provider: string; providers: string[] };
  user_metadata: Record<string, never>;
  ll: { role: Role; scenario: string; session: string };
};

/** 2100-01-01T00:00:00Z: the fake session never needs a refresh. */
export const TOKEN_EXP = 4102444800;
const TOKEN_IAT = 1792000000;

const USER_IDS: Record<Role, string> = {
  CONTROLLER: "00000000-0000-4000-a000-000000000001",
  REVIEWER: "00000000-0000-4000-a000-000000000002",
  OPS: "00000000-0000-4000-a000-000000000003",
  STRANGER: "00000000-0000-4000-a000-000000000004",
};

function b64url(text: string): string {
  return Buffer.from(text, "utf8").toString("base64url");
}

/**
 * A JWT-shaped, unsigned access token: `base64url(header).base64url(claims).base64url(sig)`.
 * Header `{"alg":"HS256","typ":"JWT"}` with no `kid`, so supabase-js `getClaims()` falls back
 * to `GET /auth/v1/user` (answered by this mock) instead of verifying a signature; `exp` is
 * 2100-01-01, so no refresh is ever due. Built at run time (no token literal is committed).
 */
export function makeAccessToken(
  role: Role,
  scenario: string,
  session: string,
  supabaseUrl: string,
): string {
  const claims: TokenClaims = {
    sub: USER_IDS[role],
    email: EMAILS[role],
    aud: "authenticated",
    role: "authenticated",
    exp: TOKEN_EXP,
    iat: TOKEN_IAT,
    iss: `${supabaseUrl.replace(/\/$/, "")}/auth/v1`,
    session_id: session,
    app_metadata: { provider: "email", providers: ["email"] },
    user_metadata: {},
    ll: { role, scenario, session },
  };
  const header = { alg: "HS256", typ: "JWT" };
  return [b64url(JSON.stringify(header)), b64url(JSON.stringify(claims)), b64url("mock")].join(
    ".",
  );
}

/** The claims of a `makeAccessToken` token (or a `Bearer ...` header); null otherwise. */
export function parseAccessToken(value: string | null | undefined): TokenClaims | null {
  if (!value) return null;
  const token = value.replace(/^Bearer\s+/i, "").trim();
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const claims = JSON.parse(Buffer.from(parts[1], "base64url").toString("utf8")) as TokenClaims;
    if (!claims.ll || !ROLES.includes(claims.ll.role) || typeof claims.ll.session !== "string") {
      return null;
    }
    return claims;
  } catch {
    return null;
  }
}

/** The Supabase user object for a token (`GET /auth/v1/user`, and inside a session). */
export function supabaseUser(claims: TokenClaims): Record<string, unknown> {
  return {
    id: claims.sub,
    aud: "authenticated",
    role: "authenticated",
    email: claims.email,
    email_confirmed_at: "2026-10-01T00:00:00Z",
    app_metadata: claims.app_metadata,
    user_metadata: {},
    identities: [],
    created_at: "2026-10-01T00:00:00Z",
    updated_at: "2026-10-01T00:00:00Z",
  };
}

/** A Supabase session (`POST /auth/v1/token`, and the SSR auth cookie's content). */
export function supabaseSession(
  role: Role,
  scenario: string,
  session: string,
  supabaseUrl: string,
): Record<string, unknown> {
  const accessToken = makeAccessToken(role, scenario, session, supabaseUrl);
  const claims = parseAccessToken(accessToken) as TokenClaims;
  return {
    access_token: accessToken,
    token_type: "bearer",
    expires_in: TOKEN_EXP - TOKEN_IAT,
    expires_at: TOKEN_EXP,
    refresh_token: ["mock-refresh", role, scenario, session].join("."),
    user: supabaseUser(claims),
  };
}

/** The role, scenario and session in a `mock-refresh.<role>.<scenario>.<session>` token. */
export function parseRefreshToken(
  value: string,
): { role: Role; scenario: string; session: string } | null {
  const parts = value.split(".");
  if (parts.length !== 4 || parts[0] !== "mock-refresh") return null;
  const role = parts[1] as Role;
  return ROLES.includes(role) ? { role, scenario: parts[2], session: parts[3] } : null;
}

/** The role an email signs in as (password / OTP); STRANGER when not on the seeded list. */
export function roleForEmail(email: string): Role {
  const found = ROLES.find((r) => EMAILS[r] === email.trim().toLowerCase());
  return found ?? "STRANGER";
}

// --- routing (pure) ---------------------------------------------------------------------

function segments(path: string): string[] {
  return path.split("/").filter((s) => s.length > 0);
}

/** Whether `path` fits `template` (`{name}` matches one non-empty segment). */
export function matchesTemplate(template: string, path: string): boolean {
  const t = segments(template);
  const p = segments(path);
  return t.length === p.length && t.every((seg, i) => /^\{.+\}$/.test(seg) || seg === p[i]);
}

export type FoundEndpoint = { key: string; endpoint: string; template: string };

/** `"METHOD /template"` and EP id of the endpoint `method path` belongs to, or null. */
export function findEndpoint(
  index: FixtureIndex,
  method: string,
  path: string,
): FoundEndpoint | null {
  const m = method.toUpperCase();
  for (const meta of Object.values(index.fixtures)) {
    if (meta.method === m && matchesTemplate(meta.template, path)) {
      return { key: `${m} ${meta.template}`, endpoint: meta.endpoint, template: meta.template };
    }
  }
  return null;
}

/** The scenario fixture for a request (rule 5 in the header), or null. */
export function pickFixture(
  index: FixtureIndex,
  scenario: string,
  method: string,
  path: string,
  query: Record<string, string>,
): string | null {
  const names = index.scenarios[scenario];
  if (!names) throw new Error(`unknown mock scenario "${scenario}"`);
  const want = segments(path);
  let best: string | null = null;
  let bestScore = -1;
  for (const name of names) {
    const meta = index.fixtures[name];
    if (!meta || meta.method !== method.toUpperCase() || !matchesTemplate(meta.template, path)) {
      continue;
    }
    const params = Object.entries(meta.query);
    if (!params.every(([k, v]) => query[k] === v)) continue;
    const have = segments(meta.path);
    const equal = want.filter((seg, i) => have[i] === seg).length;
    const score = equal * 100 + params.length;
    if (score > bestScore) {
      best = name;
      bestScore = score;
    }
  }
  return best;
}

/** Looks an override up by `METHOD /template` first, then by EP id. */
export function findOverride(
  overrides: Overrides,
  found: FoundEndpoint,
): { key: string; override: Override } | null {
  if (found.key in overrides) return { key: found.key, override: overrides[found.key] };
  if (found.endpoint in overrides) {
    return { key: found.endpoint, override: overrides[found.endpoint] };
  }
  return null;
}

/** The reply at position `n` of an override (sequences repeat their last entry). */
export function nthReply(override: Override, n: number): Reply {
  if (!Array.isArray(override)) return override;
  if (override.length === 0) throw new Error("empty override sequence");
  return override[Math.min(n, override.length - 1)];
}

export type FixtureLoader = (name: string) => unknown;

type PlainReply = { status: number; body: unknown; fixture: string | null; delayMs: number };

function replyOf(reply: Reply, index: FixtureIndex, load: FixtureLoader): PlainReply {
  const r = typeof reply === "string" ? { fixture: reply } : reply;
  if (r.fixture !== undefined) {
    const meta = index.fixtures[r.fixture];
    if (!meta) throw new Error(`unknown fixture "${r.fixture}"`);
    return {
      status: r.status ?? meta.status,
      body: r.body !== undefined ? r.body : load(r.fixture),
      fixture: r.fixture,
      delayMs: r.delayMs ?? 0,
    };
  }
  return { status: r.status ?? 200, body: r.body ?? null, fixture: null, delayMs: r.delayMs ?? 0 };
}

function mockError(status: number, code: string, message: string): ResolvedReply {
  return {
    status,
    body: { error: { code, message, stage: null, details: {} } },
    fixture: null,
    endpoint: null,
    delayMs: 0,
  };
}

/**
 * The reply to one API request (rules 1 to 6 in the header). `counters` holds how many times
 * each override key was served in this session (mutated: sequences advance).
 */
export function resolveReply(
  index: FixtureIndex,
  config: SessionConfig,
  counters: Map<string, number>,
  req: MockRequest,
  load: FixtureLoader,
): ResolvedReply {
  const found = findEndpoint(index, req.method, req.path);
  if (!found) {
    return mockError(404, "MOCK_NO_ROUTE", `mock: no endpoint for ${req.method} ${req.path}`);
  }
  const withEndpoint = (r: PlainReply): ResolvedReply => ({ ...r, endpoint: found.endpoint });
  if (req.token === null && found.endpoint !== "EP-01") {
    return withEndpoint(replyOf("ep02-unauthenticated", index, load));
  }
  const hit = findOverride(config.overrides, found);
  if (hit) {
    const n = counters.get(hit.key) ?? 0;
    counters.set(hit.key, n + 1);
    return withEndpoint(replyOf(nthReply(hit.override, n), index, load));
  }
  if (found.endpoint === "EP-02" && req.token) {
    const role = req.token.ll.role;
    const name = role === "STRANGER" ? "ep02-not-allowed" : `ep02-me-${role.toLowerCase()}`;
    return withEndpoint(replyOf(name, index, load));
  }
  const name = pickFixture(index, config.scenario, req.method, req.path, req.query);
  if (name === null) {
    const message = `mock: scenario "${config.scenario}" has no fixture for ${found.key} (${req.path})`;
    return { ...mockError(501, "MOCK_NO_FIXTURE", message), endpoint: found.endpoint };
  }
  return withEndpoint(replyOf(name, index, load));
}

// --- Supabase Auth (pure) ---------------------------------------------------------------

/**
 * Answers a Supabase Auth request: `GET /auth/v1/user` (the token's user; 401 for a token
 * that is not ours), `POST /auth/v1/token` (`grant_type=password`: a session for the email's
 * role in `defaults.scenario`/`defaults.session`; `refresh_token`: the same session again),
 * `POST /auth/v1/otp` (200, "link sent"), `POST /auth/v1/logout` (204).
 */
export function supabaseReply(
  method: string,
  path: string,
  query: Record<string, string>,
  body: unknown,
  authorization: string | undefined,
  supabaseUrl: string,
  defaults: { scenario: string; session: string },
): { status: number; body: unknown } {
  const p = path.replace(/\/$/, "");
  const m = method.toUpperCase();
  if (p === "/auth/v1/user" && m === "GET") {
    const claims = parseAccessToken(authorization);
    if (!claims) {
      return { status: 401, body: { code: 401, error_code: "bad_jwt", msg: "invalid JWT" } };
    }
    return { status: 200, body: supabaseUser(claims) };
  }
  if (p === "/auth/v1/token" && m === "POST") {
    const b = (body ?? {}) as Record<string, string>;
    if (query.grant_type === "refresh_token") {
      const parsed = parseRefreshToken(b.refresh_token ?? "");
      if (!parsed) {
        const msg = "Invalid Refresh Token";
        return { status: 400, body: { code: 400, error_code: "refresh_token_not_found", msg } };
      }
      const s = supabaseSession(parsed.role, parsed.scenario, parsed.session, supabaseUrl);
      return { status: 200, body: s };
    }
    const role = roleForEmail(b.email ?? "");
    return {
      status: 200,
      body: supabaseSession(role, defaults.scenario, defaults.session, supabaseUrl),
    };
  }
  if ((p === "/auth/v1/otp" || p === "/auth/v1/magiclink") && m === "POST") {
    return { status: 200, body: {} };
  }
  if (p === "/auth/v1/logout" && m === "POST") return { status: 204, body: null };
  return {
    status: 404,
    body: { code: 404, error_code: "mock_no_route", msg: `mock: ${m} ${p}` },
  };
}

// --- fixtures on disk -------------------------------------------------------------------

/** Reads fixture files from `dir`, re-reading any whose modification time changed. */
export function fixtureStore(dir: string): { index: () => FixtureIndex; load: FixtureLoader } {
  const cache = new Map<string, { mtimeMs: number; value: unknown }>();
  const read = (file: string): unknown => {
    const path = join(dir, file);
    const mtimeMs = statSync(path).mtimeMs;
    const hit = cache.get(file);
    if (hit && hit.mtimeMs === mtimeMs) return hit.value;
    const value: unknown = JSON.parse(readFileSync(path, "utf8"));
    cache.set(file, { mtimeMs, value });
    return value;
  };
  const index = (): FixtureIndex => read("index.json") as FixtureIndex;
  return {
    index,
    load: (name: string) => {
      const meta = index().fixtures[name];
      if (!meta) throw new Error(`unknown fixture "${name}"`);
      return read(meta.file);
    },
  };
}

// --- the HTTP server --------------------------------------------------------------------

export type MockServerOptions = {
  fixturesDir: string;
  apiPort: number;
  supabasePort: number;
  supabaseUrl: string;
  version: string;
  idleMs: number;
  host?: string;
};

type Session = { config: SessionConfig; counters: Map<string, number>; calls: MockCall[] };

/** Short hash of this file's source: a running server of another version is replaced. */
export function sourceVersion(source: string): string {
  return createHash("sha256").update(source).digest("hex").slice(0, 16);
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

/** JSON body, or the raw text when it is not JSON (form posts), or null when empty. */
export function parseBody(text: string): unknown {
  if (text === "") return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

const CORS: Record<string, string> = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "*",
  "access-control-allow-methods": "GET, POST, PUT, DELETE, OPTIONS",
};

function send(res: ServerResponse, status: number, body: unknown): void {
  if (status === 204 || body === null) {
    res.writeHead(status, CORS);
    res.end();
    return;
  }
  res.writeHead(status, { "content-type": "application/json", ...CORS });
  res.end(JSON.stringify(body));
}

const SESSION_PATH = /^\/__mock\/sessions\/([^/]+)(\/calls)?$/;

/** Starts the mock on both ports; resolves once both listen (rejects, closed, on EADDRINUSE). */
export async function startMockServer(
  opts: MockServerOptions,
): Promise<{ close: () => Promise<void> }> {
  const host = opts.host ?? "127.0.0.1";
  const store = fixtureStore(opts.fixturesDir);
  const sessions = new Map<string, Session>();
  const servers: Server[] = [];
  let seq = 0;
  let idle: ReturnType<typeof setTimeout> | null = null;

  const close = async () => {
    if (idle) clearTimeout(idle);
    await Promise.all(servers.map((s) => new Promise<void>((r) => s.close(() => r()))));
  };
  const exit = () => void close().then(() => process.exit(0));
  const touch = () => {
    if (idle) clearTimeout(idle);
    idle = setTimeout(exit, opts.idleMs);
  };
  const sessionOf = (id: string, scenario: string): Session => {
    let s = sessions.get(id);
    if (!s) {
      s = { config: { scenario, overrides: {} }, counters: new Map(), calls: [] };
      sessions.set(id, s);
    }
    return s;
  };

  const handle = async (req: IncomingMessage, res: ServerResponse, port: "api" | "supabase") => {
    touch();
    const url = new URL(req.url ?? "/", `http://${host}`);
    const query = Object.fromEntries(url.searchParams.entries());
    const method = (req.method ?? "GET").toUpperCase();
    if (method === "OPTIONS") return send(res, 204, null);
    const body = parseBody(await readBody(req));

    if (url.pathname === "/__mock/health") {
      const { version, apiPort, supabasePort } = opts;
      return send(res, 200, { ok: true, version, apiPort, supabasePort });
    }
    if (url.pathname === "/__mock/shutdown" && method === "POST") {
      send(res, 200, { ok: true });
      setTimeout(exit, 10);
      return;
    }
    const control = url.pathname.match(SESSION_PATH);
    if (control) {
      const id = decodeURIComponent(control[1]);
      if (control[2] && method === "GET") return send(res, 200, sessions.get(id)?.calls ?? []);
      if (!control[2] && method === "PUT") {
        const cfg = body as SessionConfig;
        if (!store.index().scenarios[cfg.scenario]) {
          return send(res, 400, { error: `unknown scenario "${cfg.scenario}"` });
        }
        const s = sessionOf(id, cfg.scenario);
        s.config = { scenario: cfg.scenario, overrides: cfg.overrides ?? {} };
        s.counters = new Map();
        return send(res, 200, s.config);
      }
      if (!control[2] && method === "DELETE") {
        sessions.delete(id);
        return send(res, 204, null);
      }
    }

    if (port === "supabase" || url.pathname.startsWith("/auth/v1/")) {
      const defaults = { scenario: DEFAULT_SCENARIO, session: "anonymous" };
      const auth = req.headers.authorization;
      const reply = supabaseReply(method, url.pathname, query, body, auth, opts.supabaseUrl, defaults);
      return send(res, reply.status, reply.body);
    }
    if (!url.pathname.startsWith(API_PREFIX + "/")) {
      const error = { code: "MOCK_NO_ROUTE", message: `mock: ${url.pathname}` };
      return send(res, 404, { error });
    }

    const path = url.pathname.slice(API_PREFIX.length);
    const token = parseAccessToken(req.headers.authorization);
    const fromHeader = req.headers["x-mock-session"];
    const sessionId = token?.ll.session ?? (typeof fromHeader === "string" ? fromHeader : "anonymous");
    const session = sessionOf(sessionId, token?.ll.scenario ?? DEFAULT_SCENARIO);
    let reply: ResolvedReply;
    try {
      const request = { method, path, query, token };
      reply = resolveReply(store.index(), session.config, session.counters, request, store.load);
    } catch (e) {
      reply = mockError(500, "MOCK_ERROR", `mock: ${(e as Error).message}`);
    }
    session.calls.push({
      method,
      path,
      query,
      body,
      endpoint: reply.endpoint,
      fixture: reply.fixture,
      status: reply.status,
      via: req.headers["x-mock-via"] === "browser" ? "browser" : "server",
      seq: Date.now() * 1000 + (++seq % 1000),
    });
    if (reply.delayMs > 0) await new Promise((r) => setTimeout(r, reply.delayMs));
    send(res, reply.status, reply.body);
  };

  const listen = (port: number, kind: "api" | "supabase") =>
    new Promise<void>((resolve, reject) => {
      const server = createServer((req, res) => {
        handle(req, res, kind).catch((e: unknown) => {
          send(res, 500, { error: { code: "MOCK_ERROR", message: String(e) } });
        });
      });
      server.once("error", reject);
      server.listen(port, host, () => {
        servers.push(server);
        resolve();
      });
    });

  try {
    await listen(opts.apiPort, "api");
    await listen(opts.supabasePort, "supabase");
  } catch (e) {
    await close();
    throw e;
  }
  touch();
  return { close };
}
