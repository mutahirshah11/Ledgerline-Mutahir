import type { NextConfig } from "next";

// Phase 5 (006-web) T018: same-origin API proxy + security headers (research R4, plan.md).
//
// `distDir` is overridable so parallel Playwright "screens" runs never share a build
// (scratchpad common rules: each agent/test run gets its own `NEXT_DIST_DIR`).
//
// `API_BASE_URL` is server-only (never a `NEXT_PUBLIC_*` var, blueprint Section 26). Two
// deployment shapes read it differently:
//  - Separate projects / local dev: it is a plain value known at build time, and this rewrite
//    proxies the browser's same-origin `/api/v1/*` to it directly.
//  - Vercel "services" (one project, apps/vercel.json): the top-level `rewrites` there already
//    routes `/api/(.*)` to the api service at the edge, before Next ever sees the request, so
//    this rewrite is unreachable for browser calls; `API_BASE_URL` instead comes from a service
//    *binding*, which resolves only at request time in a function (lib/server-api.ts), never at
//    build time — so the build must not require it here.

function buildConfig(): NextConfig {
  const apiBaseUrl = process.env.API_BASE_URL;

  return {
    distDir: process.env.NEXT_DIST_DIR || ".next",

    async rewrites() {
      if (!apiBaseUrl) return [];
      return [
        {
          source: "/api/v1/:path*",
          destination: `${apiBaseUrl}/api/v1/:path*`,
        },
      ];
    },

    async headers() {
      return [
        {
          source: "/:path*",
          headers: [
            { key: "Referrer-Policy", value: "no-referrer" },
            { key: "X-Frame-Options", value: "DENY" },
            { key: "X-Content-Type-Options", value: "nosniff" },
          ],
        },
      ];
    },
  };
}

export default buildConfig;
