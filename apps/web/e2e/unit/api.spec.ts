import { expect, test } from "@playwright/test";

import { ApiError, createApiClient, isUnreachable, resolveBaseUrl } from "../../lib/api";

// 006-web T012: lib/api.ts, the typed client over lib/api-types.ts (FR-004, FR-005, WEB-AC1,
// research R1/R4). Pure Node: a fake fetch records every request; no browser, no server.

type Call = { url: string; init: RequestInit };

function fakeFetch(respond: (call: Call) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
    const call = { url: String(input), init: init ?? {} };
    calls.push(call);
    return respond(call);
  };
  return { calls, fetchImpl: fetchImpl as typeof fetch };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function header(call: Call, name: string): string | null {
  return new Headers(call.init.headers).get(name);
}

const ME = { email: "controller@kestrel.example", display_name: "Controller", role: "CONTROLLER" };

test("FR-004 every call carries the access token as Authorization: Bearer", async () => {
  const { calls, fetchImpl } = fakeFetch(() => json(200, ME));
  const client = createApiClient({
    baseUrl: "/api/v1",
    getToken: async () => "token-abc",
    fetch: fetchImpl,
  });

  const me = await client.get("/me");

  expect(me).toEqual(ME);
  expect(calls).toHaveLength(1);
  expect(calls[0].url).toBe("/api/v1/me");
  expect(calls[0].init.method).toBe("GET");
  expect(header(calls[0], "Authorization")).toBe("Bearer token-abc");
});

test("FR-004 no session means no Authorization header (the API answers 401)", async () => {
  const { calls, fetchImpl } = fakeFetch(() => json(200, { status: "ok" }));
  const client = createApiClient({ baseUrl: "/api/v1", getToken: async () => null, fetch: fetchImpl });

  await client.get("/health");

  expect(header(calls[0], "Authorization")).toBeNull();
});

test("FR-005 base is /api/v1 in the browser and API_BASE_URL/api/v1 on the server", () => {
  expect(resolveBaseUrl({ server: false, apiBaseUrl: "http://api.internal:8000" })).toBe("/api/v1");
  expect(resolveBaseUrl({ server: false, apiBaseUrl: undefined })).toBe("/api/v1");
  expect(resolveBaseUrl({ server: true, apiBaseUrl: "http://127.0.0.1:3999" })).toBe(
    "http://127.0.0.1:3999/api/v1",
  );
  expect(resolveBaseUrl({ server: true, apiBaseUrl: "https://api.example.com/" })).toBe(
    "https://api.example.com/api/v1",
  );
  expect(() => resolveBaseUrl({ server: true, apiBaseUrl: undefined })).toThrow(/API_BASE_URL/);
});

test("FR-004 path params are encoded, query skips empty values, JSON body is sent", async () => {
  const { calls, fetchImpl } = fakeFetch(() => json(200, []));
  const client = createApiClient({
    baseUrl: "http://127.0.0.1:3999/api/v1",
    getToken: async () => "t",
    fetch: fetchImpl,
  });

  await client.get("/runs", { query: { limit: 20 } });
  await client.get("/runs/{id}/groups", {
    path: { id: "run 1/x" },
    query: { outcome: "MATCHED" },
  });
  await client.post("/runs", { body: { month: "2026-09" } });
  await client.get("/runs/{id}/groups", { path: { id: "r-1" }, query: { outcome: null } });

  expect(calls[0].url).toBe("http://127.0.0.1:3999/api/v1/runs?limit=20");
  expect(calls[1].url).toBe(
    "http://127.0.0.1:3999/api/v1/runs/run%201%2Fx/groups?outcome=MATCHED",
  );
  expect(calls[2].init.method).toBe("POST");
  expect(header(calls[2], "Content-Type")).toBe("application/json");
  expect(JSON.parse(String(calls[2].init.body))).toEqual({ month: "2026-09" });
  expect(calls[3].url).toBe("http://127.0.0.1:3999/api/v1/runs/r-1/groups");
});

test("FR-004 the Section 11 error body becomes ApiError {status, code, message, stage, details}", async () => {
  const body = {
    error: {
      code: "DUPLICATE_RUN",
      message: "A run for 2026-09 already exists",
      stage: "upload",
      details: { run_id: "r-1" },
    },
  };
  const { fetchImpl } = fakeFetch(() => json(409, body));
  const client = createApiClient({ baseUrl: "/api/v1", getToken: async () => "t", fetch: fetchImpl });

  const err = await client.post("/runs", { body: { month: "2026-09" } }).catch((e: unknown) => e);

  expect(err).toBeInstanceOf(ApiError);
  const apiErr = err as ApiError;
  expect(apiErr.status).toBe(409);
  expect(apiErr.code).toBe("DUPLICATE_RUN");
  expect(apiErr.message).toBe("A run for 2026-09 already exists");
  expect(apiErr.stage).toBe("upload");
  expect(apiErr.details).toEqual({ run_id: "r-1" });
  expect(isUnreachable(apiErr)).toBe(false);
});

test("FR-004 a 502 Section 11 error is a step failure, a 502 without that body is unreachable", async () => {
  const upstream = {
    error: { code: "UPSTREAM_FAILED", message: "Reading page 2 failed", stage: "extract", details: {} },
  };
  let reply: () => Response = () => json(502, upstream);
  const { fetchImpl } = fakeFetch(() => reply());
  const client = createApiClient({ baseUrl: "/api/v1", getToken: async () => "t", fetch: fetchImpl });

  const stepErr = (await client.get("/health").catch((e: unknown) => e)) as ApiError;
  expect(stepErr.code).toBe("UPSTREAM_FAILED");
  expect(stepErr.stage).toBe("extract");
  expect(isUnreachable(stepErr)).toBe(false);

  reply = () => new Response("<html>Bad gateway</html>", { status: 502 });
  const proxyErr = (await client.get("/health").catch((e: unknown) => e)) as ApiError;
  expect(proxyErr).toBeInstanceOf(ApiError);
  expect(proxyErr.status).toBe(502);
  expect(proxyErr.code).toBe("BAD_RESPONSE");
  expect(proxyErr.stage).toBeNull();
  expect(proxyErr.details).toEqual({});
  expect(isUnreachable(proxyErr)).toBe(true);
});

test("FR-004 a network failure is ApiError status 0 NETWORK_ERROR (unreachable)", async () => {
  const client = createApiClient({
    baseUrl: "/api/v1",
    getToken: async () => "t",
    fetch: (async () => {
      throw new TypeError("fetch failed");
    }) as typeof fetch,
  });

  const err = (await client.get("/me").catch((e: unknown) => e)) as ApiError;

  expect(err).toBeInstanceOf(ApiError);
  expect(err.status).toBe(0);
  expect(err.code).toBe("NETWORK_ERROR");
  expect(isUnreachable(err)).toBe(true);
});

test("WEB-AC1 a 401 is signalled through onUnauthorized and still rejects with ApiError", async () => {
  const body = { error: { code: "UNAUTHENTICATED", message: "Sign in again", stage: null, details: {} } };
  const { fetchImpl } = fakeFetch(() => json(401, body));
  const seen: ApiError[] = [];
  const client = createApiClient({
    baseUrl: "/api/v1",
    getToken: async () => "expired",
    fetch: fetchImpl,
    onUnauthorized: (e) => {
      seen.push(e);
    },
  });

  const err = (await client.get("/runs/{id}", { path: { id: "r-1" } }).catch((e: unknown) => e)) as ApiError;

  expect(err).toBeInstanceOf(ApiError);
  expect(err.status).toBe(401);
  expect(err.code).toBe("UNAUTHENTICATED");
  expect(seen).toHaveLength(1);
  expect(seen[0]).toBe(err);
});

test("FR-004 only the 401 hook fires on 401; other errors do not call it", async () => {
  const body = { error: { code: "NOT_FOUND", message: "Run not found", stage: null, details: {} } };
  const { fetchImpl } = fakeFetch(() => json(404, body));
  let hooked = 0;
  const client = createApiClient({
    baseUrl: "/api/v1",
    getToken: async () => "t",
    fetch: fetchImpl,
    onUnauthorized: () => {
      hooked += 1;
    },
  });

  const err = (await client.get("/runs/{id}", { path: { id: "nope" } }).catch((e: unknown) => e)) as ApiError;

  expect(err.status).toBe(404);
  expect(hooked).toBe(0);
});

test("FR-003 a 403 calls onForbidden with the method, then rejects", async () => {
  const body = { error: { code: "NOT_ALLOWED", message: "Not allowed", stage: null, details: {} } };
  const { fetchImpl } = fakeFetch(() => json(403, body));
  const seen: [number, string][] = [];
  let unauthorized = 0;
  const client = createApiClient({
    baseUrl: "/api/v1",
    getToken: async () => "t",
    fetch: fetchImpl,
    onForbidden: (e, method) => {
      seen.push([e.status, method]);
    },
    onUnauthorized: () => {
      unauthorized += 1;
    },
  });

  const getErr = (await client.get("/me").catch((e: unknown) => e)) as ApiError;
  const postErr = (await client.post("/runs", { body: { month: "2026-09" } }).catch((e: unknown) => e)) as ApiError;

  expect(getErr).toBeInstanceOf(ApiError);
  expect(getErr.code).toBe("NOT_ALLOWED");
  expect(postErr.status).toBe(403);
  expect(seen).toEqual([
    [403, "get"],
    [403, "post"],
  ]);
  expect(unauthorized).toBe(0);
});

test("FR-004 a 204 or empty 2xx body resolves to undefined", async () => {
  let reply: () => Response = () => new Response(null, { status: 204 });
  const { fetchImpl } = fakeFetch(() => reply());
  const client = createApiClient({ baseUrl: "/api/v1", getToken: async () => "t", fetch: fetchImpl });

  expect(await client.get("/health")).toBeUndefined();
  reply = () => new Response("", { status: 200 });
  expect(await client.get("/health")).toBeUndefined();
});

test("FR-004 a 2xx body that is not JSON is ApiError BAD_RESPONSE", async () => {
  const { fetchImpl } = fakeFetch(() => new Response("<html>login page</html>", { status: 200 }));
  const client = createApiClient({ baseUrl: "/api/v1", getToken: async () => "t", fetch: fetchImpl });

  const err = (await client.get("/me").catch((e: unknown) => e)) as ApiError;

  expect(err).toBeInstanceOf(ApiError);
  expect(err.status).toBe(200);
  expect(err.code).toBe("BAD_RESPONSE");
});

test("FR-004 an aborted request rejects with the AbortError itself, not ApiError", async () => {
  const controller = new AbortController();
  const client = createApiClient({
    baseUrl: "/api/v1",
    getToken: async () => "t",
    fetch: (async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(init?.signal).toBe(controller.signal);
      throw new DOMException("The operation was aborted.", "AbortError");
    }) as typeof fetch,
  });
  controller.abort();

  const err = await client.get("/me", { signal: controller.signal }).catch((e: unknown) => e);

  expect(err).not.toBeInstanceOf(ApiError);
  expect((err as DOMException).name).toBe("AbortError");
});

// Compile-time contract (checked by `pnpm typecheck`, never executed): paths, params and bodies
// come from lib/api-types.ts.
export function typeContract(client: ReturnType<typeof createApiClient>) {
  // @ts-expect-error unknown path
  void client.get("/nope");
  // @ts-expect-error EP-05 needs its {id} path param
  void client.get("/runs/{id}");
  // @ts-expect-error EP-03 needs a body
  void client.post("/runs");
  // @ts-expect-error /me has no POST
  void client.post("/me", { body: {} });
  return client.get("/me").then((me) => me.role satisfies "CONTROLLER" | "REVIEWER" | "OPS");
}
