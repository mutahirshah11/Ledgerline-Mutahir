// 006-web T016: the typed API client over lib/api-types.ts (research R1/R4, FR-004, FR-005).
//
// - Paths, path/query params, request bodies and 200/201 response bodies all come from the
//   generated `paths` (openapi-typescript); callers write the path without the `/api/v1` prefix:
//     api.get("/runs/{id}", { path: { id } })
// - Base: `/api/v1` in the browser (same-origin rewrite in next.config.ts), `${API_BASE_URL}/api/v1`
//   on the server (lib/server-api.ts).
// - Every call carries `Authorization: Bearer <access token>` from the token provider.
// - Any non-2xx becomes `ApiError {status, code, message, stage, details}` (Section 11 body
//   `{"error": {...}}`); a 401 also calls `onUnauthorized` (browser: go to /login?next=, WEB-AC1).

import type { components, paths } from "./api-types";
import { loginUrl } from "./me";
import { getBrowserSupabase } from "./supabase";

// --- errors -----------------------------------------------------------------------------------

/** Section 11 `ErrorCode` (apps/api/app/domain/enums.py; not in the OpenAPI schema). */
export type ErrorCode =
  | "BAD_MONTH"
  | "MISSING_SOURCE"
  | "BAD_REQUEST"
  | "CONFIRM_REQUIRED"
  | "UNAUTHENTICATED"
  | "NOT_ALLOWED"
  | "NOT_FOUND"
  | "RUN_STATE_CONFLICT"
  | "DUPLICATE_RUN"
  | "PROOF_NOT_PASSED"
  | "ITEM_ALREADY_RESOLVED"
  | "CONTRACT_VIOLATION"
  | "INVALID_REVIEW_ACTION"
  | "UPSTREAM_FAILED"
  | "INTERNAL_ERROR";

/** Client-side codes: the request never reached the API (`NETWORK_ERROR`, status 0) or the reply
 * was not a Section 11 body (`BAD_RESPONSE`, e.g. the proxy's own 502 page). */
export type ClientErrorCode = "NETWORK_ERROR" | "BAD_RESPONSE";

export type Stage = components["schemas"]["Stage"];

export class ApiError extends Error {
  readonly status: number;
  readonly code: ErrorCode | ClientErrorCode;
  readonly stage: Stage | null;
  readonly details: Record<string, unknown>;

  constructor(init: {
    status: number;
    code: ErrorCode | ClientErrorCode;
    message: string;
    stage?: Stage | null;
    details?: Record<string, unknown>;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.stage = init.stage ?? null;
    this.details = init.details ?? {};
  }
}

/** True when Ledgerline itself could not be reached (show "Ledgerline is not reachable right
 * now"), as opposed to the API answering with a Section 11 error such as UPSTREAM_FAILED. */
export function isUnreachable(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  return err.code === "NETWORK_ERROR" || (err.code === "BAD_RESPONSE" && err.status >= 500);
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

async function toApiError(res: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  const e = isRecord(body) ? body.error : undefined;
  if (isRecord(e) && typeof e.code === "string" && typeof e.message === "string") {
    return new ApiError({
      status: res.status,
      code: e.code as ErrorCode,
      message: e.message,
      stage: typeof e.stage === "string" ? (e.stage as Stage) : null,
      details: isRecord(e.details) ? e.details : {},
    });
  }
  return new ApiError({
    status: res.status,
    code: "BAD_RESPONSE",
    message: `Unexpected response (HTTP ${res.status})`,
  });
}

// --- types derived from lib/api-types.ts ------------------------------------------------------

export type Method = "get" | "post" | "put" | "patch" | "delete";

type StripPrefix<P> = P extends `/api/v1${infer R}` ? R : never;
/** Every API path without the `/api/v1` prefix, e.g. "/runs/{id}". */
export type ApiPath = StripPrefix<keyof paths>;
type FullPath<P extends ApiPath> = `/api/v1${P}` & keyof paths;

/** Paths that have an operation for method M. */
export type PathsWith<M extends Method> = {
  [P in ApiPath]: undefined extends paths[FullPath<P>][M] ? never : P;
}[ApiPath];

type Op<P extends ApiPath, M extends Method> = NonNullable<paths[FullPath<P>][M]>;

type PathParamsOf<O> = O extends { parameters: { path: infer X } } ? X : never;
type QueryOf<O> = O extends { parameters: { query?: infer Q } } ? NonNullable<Q> : never;
type BodyOf<O> = O extends { requestBody: { content: { "application/json": infer B } } }
  ? { required: true; body: B }
  : O extends { requestBody?: never }
    ? never
    : O extends { requestBody?: { content: { "application/json": infer B } } }
      ? { required: false; body: B }
      : never;

type PathPart<O> = [PathParamsOf<O>] extends [never] ? { path?: never } : { path: PathParamsOf<O> };
type QueryPart<O> = [QueryOf<O>] extends [never] ? { query?: never } : { query?: QueryOf<O> };
type BodyPart<O> = [BodyOf<O>] extends [never]
  ? { body?: never }
  : BodyOf<O> extends { required: true; body: infer B }
    ? { body: B }
    : BodyOf<O> extends { body: infer B }
      ? { body?: B }
      : never;

export type RequestOptions<O> = PathPart<O> &
  QueryPart<O> &
  BodyPart<O> & {
    signal?: AbortSignal;
  };

type JsonOf<R> = R extends { content: { "application/json": infer T } } ? T : undefined;
/** The success body (200, else 201) of an operation. */
export type ResponseOf<O> = O extends { responses: infer R }
  ? R extends { 200: infer S }
    ? JsonOf<S>
    : R extends { 201: infer S }
      ? JsonOf<S>
      : undefined
  : never;

type Args<O> = object extends RequestOptions<O> ? [opts?: RequestOptions<O>] : [opts: RequestOptions<O>];

/** Response type of `api.<method>(path)`, e.g. `ApiResponse<"get", "/runs/{id}">` is RunView. */
export type ApiResponse<M extends Method, P extends PathsWith<M>> = ResponseOf<Op<P, M>>;

// --- client -----------------------------------------------------------------------------------

export type ApiClientOptions = {
  /** "/api/v1" (browser) or "${API_BASE_URL}/api/v1" (server); see resolveBaseUrl. */
  baseUrl: string;
  /** The Supabase access token, or null when signed out (then no Authorization header). */
  getToken: () => Promise<string | null>;
  fetch?: typeof fetch;
  /** Called with the error before a 401 rejects (WEB-AC1). */
  onUnauthorized?: (err: ApiError) => void;
  /** Called with the error before a 403 rejects (server reads use it for /forbidden). */
  onForbidden?: (err: ApiError) => void;
};

/** The API has only GET and POST operations today; add a method here when one appears. */
export type ApiClient = {
  get<P extends PathsWith<"get">>(path: P, ...args: Args<Op<P, "get">>): Promise<ResponseOf<Op<P, "get">>>;
  post<P extends PathsWith<"post">>(path: P, ...args: Args<Op<P, "post">>): Promise<ResponseOf<Op<P, "post">>>;
};

type LooseOptions = {
  path?: Record<string, string | number>;
  query?: Record<string, string | number | boolean | null | undefined>;
  body?: unknown;
  signal?: AbortSignal;
};

function buildUrl(baseUrl: string, path: string, opts: LooseOptions): string {
  const filled = path.replace(/\{([^}]+)\}/g, (_, name: string) => {
    const v = opts.path?.[name];
    if (v === undefined) throw new Error(`Missing path param "${name}" for ${path}`);
    return encodeURIComponent(String(v));
  });
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(opts.query ?? {})) {
    if (v !== undefined && v !== null) qs.append(k, String(v));
  }
  const q = qs.toString();
  return `${baseUrl}${filled}${q ? `?${q}` : ""}`;
}

export function createApiClient(options: ApiClientOptions): ApiClient {
  const doFetch = options.fetch ?? ((input, init) => fetch(input, init));

  async function request(method: Method, path: string, opts: LooseOptions = {}): Promise<unknown> {
    const url = buildUrl(options.baseUrl, path, opts);
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = await options.getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    let body: string | undefined;
    if (opts.body !== undefined) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(opts.body);
    }

    let res: Response;
    try {
      res = await doFetch(url, { method: method.toUpperCase(), headers, body, signal: opts.signal });
    } catch (cause) {
      if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
      throw new ApiError({ status: 0, code: "NETWORK_ERROR", message: "Network request failed" });
    }

    if (!res.ok) {
      const err = await toApiError(res);
      if (res.status === 401) options.onUnauthorized?.(err);
      if (res.status === 403) options.onForbidden?.(err);
      throw err;
    }
    if (res.status === 204) return undefined;
    const text = await res.text();
    return text ? JSON.parse(text) : undefined;
  }

  const bind =
    (method: Method) =>
    (path: string, opts?: LooseOptions): Promise<unknown> =>
      request(method, path, opts);

  return {
    get: bind("get"),
    post: bind("post"),
  } as unknown as ApiClient;
}

/** `/api/v1` in the browser; `${API_BASE_URL}/api/v1` on the server (research R1). */
export function resolveBaseUrl(env: { server: boolean; apiBaseUrl: string | undefined }): string {
  if (!env.server) return "/api/v1";
  if (!env.apiBaseUrl) throw new Error("API_BASE_URL is not set (server-side API calls need it)");
  return `${env.apiBaseUrl.replace(/\/+$/, "")}/api/v1`;
}

// --- browser client ---------------------------------------------------------------------------

let redirecting = false;

/** Browser 401 handler: go to sign-in and come back here afterwards (WEB-AC1). */
export function redirectToLogin(): void {
  if (typeof window === "undefined" || redirecting) return;
  redirecting = true;
  window.location.assign(loginUrl(window.location.pathname + window.location.search));
}

async function browserToken(): Promise<string | null> {
  const { data } = await getBrowserSupabase().auth.getSession();
  return data.session?.access_token ?? null;
}

/** The browser client for client components (`"use client"`): same-origin `/api/v1`, the
 * Supabase session token per call, 401 → `/login?next=<current path>`. Server components use
 * `serverApi()` from lib/server-api.ts instead. */
export const api: ApiClient = createApiClient({
  baseUrl: "/api/v1",
  getToken: browserToken,
  onUnauthorized: redirectToLogin,
});
