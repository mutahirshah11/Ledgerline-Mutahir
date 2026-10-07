import type { NextConfig } from "next";
import { PHASE_PRODUCTION_BUILD } from "next/constants";

// Phase 5 (006-web) T018: same-origin API proxy + security headers (research R4, plan.md).
//
// `distDir` is overridable so parallel Playwright "screens" runs never share a build
// (scratchpad common rules: each agent/test run gets its own `NEXT_DIST_DIR`).
//
// `API_BASE_URL` is server-only (never a `NEXT_PUBLIC_*` var, blueprint Section 26) and is read
// here, at build/start time, by the rewrite. `next build` fails loudly if it is unset so a
// misconfigured deploy cannot silently proxy to the wrong place; `next lint` / `tsc --noEmit` must
// still work with no env at all, so the check only runs for the production build phase (passed as
// `phase`, not `process.env.NEXT_PHASE` — Turbopack does not set that env var at config-load time).

function buildConfig(phase: string): NextConfig {
  const apiBaseUrl = process.env.API_BASE_URL;

  if (phase === PHASE_PRODUCTION_BUILD && !apiBaseUrl) {
    throw new Error(
      "API_BASE_URL is required to build apps/web (next.config.ts rewrites /api/v1/* to it).",
    );
  }

  return {
    distDir: process.env.NEXT_DIST_DIR || ".next",

    async rewrites() {
      return [
        {
          source: "/api/v1/:path*",
          destination: `${apiBaseUrl ?? "http://127.0.0.1:3999"}/api/v1/:path*`,
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
