// 006-web T027 (US1): exchanges the magic-link code for a session, then redirects to `next`
// (lib/me.ts safeNext — never another origin, never back to a sign-in page) or `/runs`.
//
// Route Handlers can write cookies via `cookies()` from `next/headers`
// (node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/route.md "Cookies"),
// unlike server components, so this needs no response-cookie plumbing like proxy.ts.
//
// Checks EP-02 itself before picking the redirect target (same reasoning as app/login/page.tsx's
// password form): `next`'s own first read is not allowlist-gated the way EP-02 is, so landing
// there first and letting the client catch a 403 afterwards would still let that first read
// through (SCR-01-AC3 "no run data is requested" for someone off the allowlist).

import { cookies } from "next/headers";
import { NextResponse, type NextRequest } from "next/server";

import { ApiError } from "@/lib/api";
import { safeNext } from "@/lib/me";
import { serverApi } from "@/lib/server-api";
import { createServerSupabase } from "@/lib/supabase";

export async function GET(request: NextRequest): Promise<NextResponse> {
  const { searchParams } = new URL(request.url);
  const code = searchParams.get("code");
  const next = safeNext(searchParams.get("next"));

  if (code) {
    const supabase = createServerSupabase(await cookies());
    await supabase.auth.exchangeCodeForSession(code).catch(() => undefined);
  }

  let target = next;
  try {
    const client = await serverApi();
    await client.get("/me");
  } catch (err) {
    // serverApi()'s own onUnauthorized/onForbidden-for-GET both call next/navigation's
    // `redirect()`, which throws a special (non-ApiError) value that must keep propagating, not
    // be swallowed here.
    if (!(err instanceof ApiError)) throw err;
    if (err.status === 403) target = "/forbidden";
    // Any other ApiError (network, 5xx): fall through to `next`, whose own error state takes over.
  }

  return NextResponse.redirect(new URL(target, request.url));
}
