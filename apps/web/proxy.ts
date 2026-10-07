// 006-web T019 (FR-001, research R3): Next 16 renamed `middleware.ts` to `proxy.ts`
// (node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/proxy.md); same job.
//
// On every page request: refresh the Supabase session cookies via @supabase/ssr (`getUser()`
// checks the token with Supabase Auth and refreshes it when needed), then send signed-out
// visitors of any page except the sign-in pages (lib/me.ts isSignedOutPath) to
// `/login?next=<path>`. `/api/*` is not matched: the API checks the bearer token itself and an
// HTML redirect would break fetch calls.

import { NextResponse, type NextRequest } from "next/server";

import { isSignedOutPath, loginUrl } from "./lib/me";
import { createServerSupabase, type CookieToSet } from "./lib/supabase";

export async function proxy(request: NextRequest) {
  let response = NextResponse.next({ request });
  let refreshed: CookieToSet[] = [];
  let extraHeaders: Record<string, string> = {};

  const supabase = createServerSupabase({ getAll: () => request.cookies.getAll() }, (cookiesToSet, headers) => {
    refreshed = cookiesToSet;
    extraHeaders = headers;
    for (const { name, value } of cookiesToSet) request.cookies.set(name, value);
    response = NextResponse.next({ request });
    for (const { name, value, options } of cookiesToSet) response.cookies.set(name, value, options);
    for (const [k, v] of Object.entries(headers)) response.headers.set(k, v);
  });

  let signedIn = false;
  try {
    const { data } = await supabase.auth.getUser();
    signedIn = data.user !== null;
  } catch {
    signedIn = false;
  }

  const { pathname, search } = request.nextUrl;
  if (!signedIn && !isSignedOutPath(pathname)) {
    const redirect = NextResponse.redirect(new URL(loginUrl(pathname + search), request.url));
    for (const { name, value, options } of refreshed) redirect.cookies.set(name, value, options);
    for (const [k, v] of Object.entries(extraHeaders)) redirect.headers.set(k, v);
    redirect.headers.set("Cache-Control", "private, no-store");
    return redirect;
  }
  return response;
}

export const config = {
  matcher: [
    // Everything except the API rewrite, Next internals and static files.
    "/((?!api/|_next/static|_next/image|favicon\\.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico|txt|xml|webmanifest)$).*)",
  ],
};
