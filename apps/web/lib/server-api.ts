// 006-web T016/T020: the API client for server components (research R2/R4). Server components do
// the first read for SCR-02, SCR-04, SCR-05, SCR-07, SCR-08 straight to `${API_BASE_URL}/api/v1`
// with the session's access token from the request cookies.
//
//   const client = await serverApi({ next: `/runs/${id}` });
//   const run = await client.get("/runs/{id}", { path: { id } });
//
// 401 → redirect(`/login?next=<next>&expired=1`): server components cannot clear cookies, so
// /login signs out locally when it sees `expired=1`, and /login never auto-forwards a signed-in
// visitor — that is what keeps a dead session from looping.
// 403: reads redirect, posts reject. A GET 403 → redirect("/forbidden") (every role may read, so it
// means "not on the allowlist"); a POST 403 rejects with ApiError ("Your role cannot do this").
// Any other error rejects with ApiError for the screen's error state.

import "server-only";

import { cookies } from "next/headers";
import { redirect } from "next/navigation";

import { createApiClient, resolveBaseUrl, type ApiClient } from "./api";
import { loginUrl } from "./me";
import { createServerSupabase } from "./supabase";

export type ServerApiOptions = {
  /** The in-app path to come back to after sign-in (e.g. "/runs/abc/results"). */
  next?: string;
};

export async function serverApi(opts: ServerApiOptions = {}): Promise<ApiClient> {
  const supabase = createServerSupabase(await cookies());
  return createApiClient({
    baseUrl: resolveBaseUrl({ server: true, apiBaseUrl: process.env.API_BASE_URL }),
    getToken: async () => {
      // getSession only decodes the cookie; the API verifies the token itself (F-12.R3), and
      // proxy.ts already refreshed it with getUser() for this request.
      const { data } = await supabase.auth.getSession();
      return data.session?.access_token ?? null;
    },
    fetch: (input, init) => fetch(input, { ...init, cache: "no-store" }),
    onUnauthorized: () => redirect(loginUrl(opts.next ?? "/runs", { expired: true })),
    onForbidden: (_err, method) => {
      if (method === "get") redirect("/forbidden");
    },
  });
}
