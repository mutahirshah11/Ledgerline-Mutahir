// 006-web T017: Supabase clients, auth only (research R3/R4; the browser never reads tables or
// storage, F-12.R5). Sessions live in cookies via @supabase/ssr so server components and proxy.ts
// see the same session the browser wrote.
//
// No `next/headers` import here so client components can import this file; server code passes
// its cookie store in (see lib/server-api.ts and proxy.ts).

import { createBrowserClient, createServerClient, type CookieOptions } from "@supabase/ssr";
import type { SupabaseClient } from "@supabase/supabase-js";

// Literal `process.env.NEXT_PUBLIC_*` reads so Next inlines them into the browser bundle; these
// are the only two public variables (Section 26, FR-006).
function supabaseUrl(): string {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  if (!url) throw new Error("NEXT_PUBLIC_SUPABASE_URL is not set");
  return url;
}

function supabaseAnonKey(): string {
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!key) throw new Error("NEXT_PUBLIC_SUPABASE_ANON_KEY is not set");
  return key;
}

let browserClient: SupabaseClient | undefined;

/** The one browser Supabase client (sign-in, sign-out, session token for lib/api.ts). */
export function getBrowserSupabase(): SupabaseClient {
  browserClient ??= createBrowserClient(supabaseUrl(), supabaseAnonKey());
  return browserClient;
}

export type CookieToSet = { name: string; value: string; options: CookieOptions };

/** What `await cookies()` (next/headers) and `request.cookies` (proxy) both provide. */
export type CookieStoreLike = {
  getAll(): { name: string; value: string }[];
  set?(name: string, value: string, options?: CookieOptions): unknown;
};

/** A server Supabase client over a request's cookies. In server components cookies are
 * read-only, so writes are ignored there; proxy.ts refreshes and writes the session instead. */
export function createServerSupabase(
  cookieStore: CookieStoreLike,
  onSetAll?: (cookies: CookieToSet[], headers: Record<string, string>) => void,
): SupabaseClient {
  return createServerClient(supabaseUrl(), supabaseAnonKey(), {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet, headers) {
        if (onSetAll) {
          onSetAll(cookiesToSet, headers);
          return;
        }
        try {
          for (const { name, value, options } of cookiesToSet) {
            cookieStore.set?.(name, value, options);
          }
        } catch {
          // Server component: cookies are read-only; proxy.ts already refreshed the session.
        }
      },
    },
  });
}
