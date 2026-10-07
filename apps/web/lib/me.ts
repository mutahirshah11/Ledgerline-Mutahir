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

/** `/login?next=<requested path + search>`; plain `/login` when there is nowhere useful to return
 * (the home page, or a sign-in page itself). `next` is the in-app path only (no origin). */
export function loginUrl(next: string): string {
  const pathname = next.split(/[?#]/, 1)[0];
  if (pathname === "/" || pathname === "" || isSignedOutPath(pathname)) return "/login";
  return `/login?next=${encodeURIComponent(next)}`;
}
