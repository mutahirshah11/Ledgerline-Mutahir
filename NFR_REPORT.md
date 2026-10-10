# NFR report

Last measured: 2026-10-10 · Deployments measured: none yet (preview and production are not
deployed in this session; see `TIMELOG.md` and the P5 hand-off tasks) · Commit:
`f144aff54b5b9669306240a8cc30d74980ed659a`

Rows that need a live preview/production deployment or real OpenAI/Telegram keys are
`NOT MEASURED` for now (deferred alongside key setup); they will be filled in once Phase 007's
hand-off work (P4/P5) runs against a real deployment.

## Table 1: Non-functional requirements

| ID | Requirement | Target | Method | Values | Result | Evidence | Env | Date |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| NFR-01 | Upload to results, new pages | < 3 min | `run_pack.py --measure`, cache cleared before each run | | NOT MEASURED | Needs a deployed preview and a live OpenAI key; not available this session | preview | |
| NFR-02 | Upload to results, cached pages | < 30 s | same, cache populated | | NOT MEASURED | Needs a deployed production environment; not available this session | production | |
| NFR-03 | No request over Vercel limit; extraction per page worst case | each < 300 s; per page < 200 s | config: `EXTRACT_ATTEMPTS=3` x `EXTRACT_TIMEOUT_S=60` + `EXTRACT_WAITS_S` (2, 8) | 190 s (config) | NOT MEASURED | Config value is 190 s < 200 s target (`apps/api/app/core/config.py`); the longest-observed-request half needs live Vercel logs, not available this session | preview | 2026-10-10 |
| NFR-04 | Model cost per new 2-page run | < US$0.05 | tokens of the NFR-01 runs x published price | | NOT MEASURED | Depends on NFR-01's live runs | preview | |
| NFR-05 | Re-reconcile after a review decision | < 3 s | EP-15 duration from structured log, 3 decisions | | NOT MEASURED | Needs a deployed preview and a live review session | preview | |
| NFR-06 | Determinism | identical groups, diffs, report body | `test_F05_AC8_deterministic` + `test_golden` (offline, pure functions) | pass | NOT MEASURED | Offline determinism tests are green (`apps/api/tests/test_F05_reconcile.py::test_F05_AC8_deterministic`); the live two-EP-10-calls hash comparison needs a deployed preview, not available this session | ci | 2026-10-10 |
| NFR-07 | Python function bundle | < 250 MB; no pandas/numpy | `uv.lock` check (automated, `test_P6_no_pandas_numpy`); Vercel build output size | no pandas/numpy: yes | NOT MEASURED | `apps/api/uv.lock` has neither package (confirmed offline); the build-size half needs a real Vercel Production build, not available this session | production | 2026-10-10 |
| NFR-08 | Mobile layout | SCR-04 and SCR-06 usable at 380 px | manual on a phone + screenshots | | NOT MEASURED | Needs a deployed production environment | production | |
| NFR-09 | Accessibility | outcome as text and colour; actions keyboard reachable | keyboard-only pass of SCR-03 to SCR-06 | | NOT MEASURED | Needs a deployed production environment | production | |
| NFR-10 | Setup time from clone to first run | < 15 min | fresh-clone test, README only (Table 3) | | NOT MEASURED | No attempt run yet; needs a disposable Supabase project and a live OpenAI key (FR-018) | local | |
| NFR-11 | Offline test suite | passes, < 60 s, no network | CI run (`uv run pytest -m "not integration and not live"`) + 3 local runs | CI: 29.17 s; local: 140.23 s, 100.64 s, 131.92 s | NOT MET | CI (Linux, GitHub Actions run 38051402450) meets target at 29.17 s; this builder's local Windows machine does not (all 3 isolated local runs over 100 s, no other process running) — collecting/running ~1,760 tests is markedly slower on this machine than on CI's Linux runner. The offline suite still passes with zero failures and no network use in both environments | ci + local | 2026-10-10 |

Rules: `MET` only if every value is under target; any value over -> `NOT MET` with one line on why. No evidence -> `NOT MEASURED`, never `MET`.

## Table 2: Section 2 success metrics

| Metric | Target | Value | Result | Evidence |
| --- | --- | --- | --- | --- |
| Golden expectations produced | 12 of 12 | 12 of 12 | MET | `test_golden` in CI (GE-01 to GE-12 plus the counts test, `apps/api/tests/test_golden.py`); not yet cross-checked against a live deployed run |
| Review queue before review | <= 10 items | | NOT MEASURED | Needs EP-14 on a live run |
| Proof | 86 of 86 rows placed once | 86 of 86 | MET | `apps/api/tests/test_F09_report.py::test_F09_AC3_outcomes_csv_86_lines` and the row-counts sum assertion, both offline |
| End-to-end run | < 3 min | (= NFR-01) | NOT MEASURED | See NFR-01 |
| Model cost per run; cached rerun | < US$0.05; US$0 | (= NFR-04) | NOT MEASURED | See NFR-04 |
| Duplicate Telegram messages across repeated runs | 0 | | NOT MEASURED | Needs a live Telegram bot and a repeated finalise on a deployed run |

## Table 3: Fresh-clone attempts (NFR-10)

| Attempt | Date | Duration (min) | Defects found | README changes |
| --- | --- | --- | --- | --- |

No attempt has been run yet: a real attempt needs a disposable Supabase project, a live
`OPENAI_API_KEY`, and the stopwatch run described in `README.md`'s "How to run: local" section
(FR-018). This is deferred alongside the rest of the key-dependent measurements above.
