import { execFileSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";

import { expect, test } from "@playwright/test";

// 006-web T064: WEB-AC3 (spec.md) "Given the production web build, When its client bundle is
// searched, Then the only NEXT_PUBLIC_ values present are the Supabase URL and anon key; no API
// address, service key, OpenAI key or bot token appears." This is a filesystem scan of the built
// output, not a page interaction, so it lives in the `unit` project (playwright.config.ts: no
// browser, no webServer) rather than `web-cross.spec.ts`.
//
// It scans `apps/web/<NEXT_DIST_DIR or .next>/static/**` (next.config.ts's overridable distDir,
// common rules for parallel agents) and builds first if that directory is missing or empty, using
// the same env shape as playwright.config.ts's `screens` webServer so the scan is meaningful (the
// two NEXT_PUBLIC_SUPABASE_* values are expected to be literally present; the build fails loudly
// without API_BASE_URL, next.config.ts).

const WEB_ROOT = resolve(__dirname, "..", "..");
const DIST_DIR = process.env.NEXT_DIST_DIR || ".next";
const STATIC_DIR = resolve(WEB_ROOT, DIST_DIR, "static");

const BUILD_ENV = {
  API_BASE_URL: process.env.API_BASE_URL || "http://127.0.0.1:3999",
  APP_BASE_URL: process.env.APP_BASE_URL || `http://localhost:${process.env.PW_PORT ?? "3100"}`,
  NEXT_PUBLIC_SUPABASE_URL: process.env.NEXT_PUBLIC_SUPABASE_URL || "http://127.0.0.1:54321",
  NEXT_PUBLIC_SUPABASE_ANON_KEY: process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY || "test-anon-key",
  NEXT_DIST_DIR: DIST_DIR,
};

function walk(dir: string): string[] {
  let out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, entry.name);
    if (entry.isDirectory()) out = out.concat(walk(p));
    else out.push(p);
  }
  return out;
}

function hasBuiltStatic(): boolean {
  return existsSync(STATIC_DIR) && walk(STATIC_DIR).length > 0;
}

function ensureBuilt(): void {
  if (hasBuiltStatic()) return;
  // No shell string interpolation: fixed argv, env values only (never user input), matching
  // playwright.config.ts's own `pnpm build` invocation for the `screens` project's webServer.
  execFileSync("pnpm", ["build"], {
    cwd: WEB_ROOT,
    stdio: "inherit",
    shell: true,
    env: { ...process.env, ...BUILD_ENV },
  });
}

// Text-ish files worth scanning (skip binary images/fonts under static/media, which cannot embed
// a JS/env string meaningfully and can break a utf8 read).
const TEXT_EXTENSIONS = [".js", ".cjs", ".mjs", ".css", ".json", ".map"];

function readAllStaticText(): string {
  ensureBuilt();
  expect(existsSync(STATIC_DIR), `${STATIC_DIR} should exist after pnpm build`).toBe(true);
  const files = walk(STATIC_DIR).filter((f) => TEXT_EXTENSIONS.includes(f.slice(f.lastIndexOf("."))));
  expect(files.length, "the build should produce at least one static JS/CSS file to scan").toBeGreaterThan(0);
  const parts: string[] = [];
  for (const file of files) {
    try {
      parts.push(readFileSync(file, "utf8"));
    } catch {
      // unreadable as utf8 (shouldn't happen for the extensions above); skip rather than fail the scan.
    }
  }
  return parts.join("\n");
}

test.describe("WEB-AC3 production bundle never leaks server secrets or unexpected env values", () => {
  test.setTimeout(10 * 60 * 1000); // next build alone can take minutes (playwright.config.ts note)

  test("only NEXT_PUBLIC_SUPABASE_* literal values appear; no API_BASE_URL, service_role, OpenAI or bot-token pattern", () => {
    const text = readAllStaticText();

    // Sanity (non-vacuous): the two public values the build was given ARE expected to be present
    // literally, proving the scan actually reads real bundle content.
    expect(text, "NEXT_PUBLIC_SUPABASE_URL should be literally present in the client bundle").toContain(
      BUILD_ENV.NEXT_PUBLIC_SUPABASE_URL,
    );
    expect(text, "NEXT_PUBLIC_SUPABASE_ANON_KEY should be literally present in the client bundle").toContain(
      BUILD_ENV.NEXT_PUBLIC_SUPABASE_ANON_KEY,
    );

    // The negative checks this acceptance criterion is actually about.
    expect(text, "the server-only API_BASE_URL must never reach the client bundle").not.toContain(
      BUILD_ENV.API_BASE_URL,
    );
    expect(text, "no Supabase service_role key pattern").not.toMatch(/service[_-]?role/i);
    expect(text, "no OpenAI API key pattern (sk-...)").not.toMatch(/\bsk-[A-Za-z0-9_-]{20,}\b/);
    expect(text, "no Telegram-style bot token pattern (digits:35 chars)").not.toMatch(
      /\b\d{6,}:[A-Za-z0-9_-]{35}\b/,
    );
  });
});
