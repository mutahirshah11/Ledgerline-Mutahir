import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTs from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTs,
  // Override default ignores of eslint-config-next.
  globalIgnores([
    // Default ignores of eslint-config-next:
    ".next/**",
    "out/**",
    "build/**",
    "next-env.d.ts",
    // Parallel Playwright "screens" builds use their own NEXT_DIST_DIR (006-web common rules).
    ".next-*/**",
    // Generated, never hand-edited (006-web R5, T006).
    "lib/api-types.ts",
  ]),
]);

export default eslintConfig;
