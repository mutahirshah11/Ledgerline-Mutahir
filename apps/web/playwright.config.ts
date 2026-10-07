import { defineConfig, devices } from "@playwright/test";

// Phase 5 (006-web) T005: three projects, no shared state between parallel agents.
//
// - "unit": pure-TS helper tests in e2e/unit/, no browser, instant (no webServer).
// - "screens": full app (next build && next start) with the API and Supabase auth mocked by
//   e2e/support/mock-api.ts; one acceptance test per ID.
// - "smoke": the real deployed Preview app (BASE_URL env); never run from an agent (common rules).
//
// Parallel agents must not share a build: each run sets its own NEXT_DIST_DIR / PW_PORT (common
// rules; honoured by next.config.ts's `distDir` and the webServer command/url below). The
// webServer is only started when the "screens" (or no/"all") project is actually selected, so a
// `--project=unit` run never pays for a build.
const PORT = process.env.PW_PORT ?? "3100";
const APP_BASE_URL = `http://localhost:${PORT}`;
const API_BASE_URL = "http://127.0.0.1:3999";
const isCI = !!process.env.CI;

function wantsProject(name: string): boolean {
  const args = process.argv;
  const projectArgs = args.filter((a) => a === "--project" || a.startsWith("--project=")).length;
  if (projectArgs === 0) return true; // no --project filter: everything (incl. screens) may run
  return args.some((a, i) => {
    if (a === "--project") return args[i + 1] === name;
    if (a.startsWith("--project=")) return a.slice("--project=".length) === name;
    return false;
  });
}

const needsScreensServer = wantsProject("screens");

export default defineConfig({
  testDir: "e2e",
  fullyParallel: true,
  forbidOnly: isCI,
  retries: isCI ? 1 : 0,
  workers: 2,
  reporter: "list",

  projects: [
    {
      name: "unit",
      testDir: "e2e/unit",
      use: {},
    },
    {
      name: "screens",
      testDir: "e2e",
      testIgnore: ["unit/**", "smoke.spec.ts"],
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1280, height: 800 },
        baseURL: APP_BASE_URL,
      },
    },
    {
      name: "smoke",
      testMatch: "smoke.spec.ts",
      use: {
        ...devices["Desktop Chrome"],
        baseURL: process.env.BASE_URL,
      },
    },
  ],

  ...(needsScreensServer
    ? {
        webServer: {
          command: `pnpm build && pnpm exec next start -p ${PORT}`,
          url: `${APP_BASE_URL}/favicon.ico`, // public (proxy matcher skips it), unlike / which redirects to /login
          reuseExistingServer: false,
          timeout: 600_000, // next build alone takes ~3 min while parallel agents build
          env: {
            API_BASE_URL,
            APP_BASE_URL,
            NEXT_PUBLIC_SUPABASE_URL: "http://127.0.0.1:54321",
            NEXT_PUBLIC_SUPABASE_ANON_KEY: "test-anon-key",
            ENABLE_TEST_PAGES: "1",
            ...(process.env.NEXT_DIST_DIR ? { NEXT_DIST_DIR: process.env.NEXT_DIST_DIR } : {}),
          },
        },
      }
    : {}),
});
