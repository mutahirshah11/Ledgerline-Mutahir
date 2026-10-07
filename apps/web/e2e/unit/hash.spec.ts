import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import { sha256Hex } from "../../lib/hash";

// 006-web T030: lib/hash.ts (research R6, F-01.R2): SHA-256 in the browser with Web Crypto,
// lower-case hex, equal to the API's own pack hashes (apps/api/tests/pack_hashes.json).

const ROOT = resolve(__dirname, "..", "..", "..", "..");
const PACK_HASHES = JSON.parse(
  readFileSync(resolve(ROOT, "apps", "api", "tests", "pack_hashes.json"), "utf8"),
) as Record<string, string>;

test("F-01.R2 SHA-256 of t4_erp_grn.csv equals pack_hashes.json", async () => {
  const bytes = readFileSync(resolve(ROOT, "data", "kestrel-2026-09", "t4_erp_grn.csv"));
  const blob = new Blob([new Uint8Array(bytes)], { type: "text/csv" });
  expect(await sha256Hex(blob)).toBe(PACK_HASHES["t4_erp_grn.csv"]);
});

test("F-01.R2 SHA-256 is lower-case hex of 64 characters (empty input)", async () => {
  expect(await sha256Hex(new Blob([]))).toBe(
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
  );
});
