// 006-web T017: who the signed-in person is and what the screens may offer them.
//
// Role gating (F-12.R4, FR-003): CONTROLLER all; REVIEWER resolve only; OPS retry only. The API
// stays the authority — these helpers only decide which buttons are shown.
// Sign-in paths (FR-001, research R3): shared by proxy.ts, providers.tsx and lib/api.ts.

import type { components } from "./api-types";

export type Role = components["schemas"]["Role"];
export type Me = components["schemas"]["Me"];

export type Permissions = {
  createRun: boolean;
  finalise: boolean;
  resolve: boolean;
  retry: boolean;
};

/** data-model.md §2 SessionPerson: the EP-02 answer plus what the role may do. */
export type SessionPerson = {
  email: string;
  displayName: string;
  role: Role;
  can: Permissions;
};

type MaybeRole = Role | null | undefined;

export function canCreateRun(role: MaybeRole): boolean {
  return role === "CONTROLLER";
}

export function canFinalise(role: MaybeRole): boolean {
  return role === "CONTROLLER";
}

export function canResolve(role: MaybeRole): boolean {
  return role === "CONTROLLER" || role === "REVIEWER";
}

export function canRetry(role: MaybeRole): boolean {
  return role === "CONTROLLER" || role === "OPS";
}

export function permissionsFor(role: MaybeRole): Permissions {
  return {
    createRun: canCreateRun(role),
    finalise: canFinalise(role),
    resolve: canResolve(role),
    retry: canRetry(role),
  };
}

export function toSessionPerson(me: Me): SessionPerson {
  return {
    email: me.email,
    displayName: me.display_name,
    role: me.role,
    can: permissionsFor(me.role),
  };
}

/** Pages a signed-out visitor may open (FR-001). `/__test__/**` pages guard themselves with
 * ENABLE_TEST_PAGES (T023). Everything else redirects to `/login?next=`. */
const SIGNED_OUT_PREFIXES = ["/login", "/auth/callback", "/__test__"] as const;

function underPrefix(pathname: string, prefix: string): boolean {
  return pathname === prefix || pathname.startsWith(`${prefix}/`);
}

export function isSignedOutPath(pathname: string): boolean {
  return SIGNED_OUT_PREFIXES.some((p) => underPrefix(pathname, p));
}

const FALLBACK_NEXT = "/runs";

function isSignInPath(pathname: string): boolean {
  return underPrefix(pathname, "/login") || underPrefix(pathname, "/auth");
}

/** A `next` target that is safe to navigate to after sign-in: a single-slash in-app path, never
 * another origin (`//x`, `/\x`, `https://…`, `javascript:`), never control characters, and never
 * a sign-in page (`/login`, `/auth/*`, which would loop). Anything else → "/runs". */
export function safeNext(raw: string | null | undefined): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//")) return FALLBACK_NEXT;
  if (raw.includes("\\") || /[\u0000-\u001f\u007f]/.test(raw)) return FALLBACK_NEXT;
  const pathname = raw.split(/[?#]/, 1)[0];
  if (pathname === "/" || isSignInPath(pathname)) return FALLBACK_NEXT;
  return raw;
}

/** `/login?next=<requested path + search>`; plain `/login` when there is nowhere useful (or safe)
 * to return to. `expired: true` adds `expired=1` (the API answered 401): /login then signs out
 * locally first. /login must never auto-forward a signed-in visitor (that is what would loop). */
export function loginUrl(next: string, opts: { expired?: boolean } = {}): string {
  const params = new URLSearchParams();
  if (safeNext(next) === next) params.set("next", next);
  if (opts.expired) params.set("expired", "1");
  const q = params.toString();
  return q ? `/login?${q}` : "/login";
}
