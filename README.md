# Ledgerline
[![CI](https://github.com/mutahirshah11/Ledgerline-Mutahir/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mutahirshah11/Ledgerline-Mutahir/actions/workflows/ci.yml)

## What it does

A warehouse's register, ERP and store sheet never agree; reconciling them by hand takes three
days every month-end. Ledgerline uploads all four files once and runs extraction, reconciliation,
control checks and a completeness proof automatically, showing the exceptions first with every
row accounted for exactly once. An AI model only reads the register's photographed pages; every
other figure comes straight from the ERP export and store sheet.

Live link: *pending the Production deploy (see `TIMELOG.md`).*

## How to run

**Hosted** (once deployed):
- Sign in as `controller@kestrel.example` (full flow: upload, review, finalise) or
  `reviewer@kestrel.example` (review only, cannot finalise); passwords are in the reply email,
  never in this repo or the video.
- Or ask for your own email to be added to the allowlist and sign in with a magic link.
- Re-uploading a pack that was already finalised shows the duplicate-run protection with a link
  to that run.
- A red health check means the free-tier Supabase project paused itself; tell the builder.

**Local** (Windows: run these in Git Bash):

1. Prerequisites: `git`, Python 3.12, [`uv`](https://docs.astral.sh/uv/), Node 24, `pnpm`, the
   [Supabase CLI](https://supabase.com/docs/guides/cli).
2. `git clone https://github.com/mutahirshah11/Ledgerline-Mutahir.git && cd Ledgerline-Mutahir`
3. `make setup`
4. Create a Supabase project, then `supabase link` and `supabase db push` to apply
   `supabase/migrations/`.
5. Copy `apps/api/.env.example` to `apps/api/.env` and `apps/web/.env.example` to
   `apps/web/.env.local`; fill in the variables below.
6. `make seed BUILDER_EMAIL=you@example.com` adds your email as `CONTROLLER` and seeds the two
   demo accounts; in Supabase Studio under Authentication, give `controller@kestrel.example`,
   `reviewer@kestrel.example` and your own email a password.
7. `make dev`, open `http://localhost:3000`, sign in, start a run with the four files in
   `data/kestrel-2026-09/`.
8. `make test` runs the offline suite (no keys needed, under 60 seconds).

Environment variables (`app` = api, web, or both; `secret` = never commit or log it):

| Variable | App | Secret | Purpose / example |
| --- | --- | --- | --- |
| `SUPABASE_URL` | api | no | `https://xxxx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | api | yes | Bypasses RLS; from Supabase Settings, API |
| `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` | web | no | Login only; RLS denies the anon key all table access |
| `OPENAI_API_KEY` | api | yes | Project key, US$5 hard limit |
| `EXTRACT_MODEL`, `PROMPT_VERSION` | api | no | `gpt-5.6-luna`, `v3` |
| `EXTRACT_PRICE_INPUT_PER_MTOK`, `EXTRACT_PRICE_OUTPUT_PER_MTOK` | api | no | Per-1M-token price, for the read-cost estimate |
| `EXTRACT_EST_INPUT_TOKENS_PER_PAGE`, `EXTRACT_EST_OUTPUT_TOKENS_PER_PAGE` | api | no | Measured on the pack already; fine as-is |
| `TELEGRAM_BOT_TOKEN` | api | yes | From `@BotFather` |
| `TELEGRAM_CONTROLLER_CHAT_ID`, `TELEGRAM_OPS_CHAT_ID` | api | no | The only two chats the bot sends to |
| `LEDGERLINE_NOW`, `LEDGERLINE_CLOCK_MODE`, `LEDGERLINE_NOW_ANCHOR` | api | no | Scenario clock; `frozen` for local and tests |
| `ALLOW_FAULTS` | api | no | `false`; `true` only for the fault demo |
| `APP_BASE_URL`, `API_BASE_URL`, `CORS_ORIGINS` | api, web | no | `http://localhost:3000`, `http://localhost:8000`, `http://localhost:3000` |
| `BUILDER_EMAIL` | api, `make seed` only | no | `you@example.com`; never committed |
| `OPENAI_TIMEOUT_S`, `EXTRACT_ATTEMPTS`, `EXTRACT_BACKOFF_S`, `TELEGRAM_ATTEMPTS`, `TELEGRAM_BACKOFF_S`, `TELEGRAM_TIMEOUT_S`, `UPLOAD_WINDOW_S`, `DOWNLOAD_URL_S`, `JWKS_CACHE_S`, `JWKS_MIN_REFETCH_S`, `REPO_BACKEND` | api | no | Internal tuning; the defaults are correct, never change them for local setup |

Test-only, never needed for the steps above: `apps/api/.env.test` (`SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY`, `SUPABASE_TEST_PROJECT_REF`, pointed at a
second, disposable Supabase project) runs the `integration` suite against real infrastructure;
`apps/web`'s Playwright harness sets its own `BASE_URL`, `CI`, `ENABLE_TEST_PAGES`,
`NEXT_DIST_DIR`, `PW_PORT`, `SMOKE_EMAIL`, `SMOKE_PASSWORD`.

## Assumptions

- The GRN number is the match key across all three sources.
- The register shows physical truth, but no single source is treated as truth: when sources
  disagree the odd one out is named and a person decides (Q6 default).
- Returns are out of scope: a supplier return is reported as a non-receipt, not reconciled
  against a debit note (Q2 default).
- An exact unit conversion (yards to metres) counts as a match (Q4 default).
- Review before finalise is optional; any still-open items are listed as waiting at finalise (Q7
  default).
- A GRN missing from every source is reported as a sequence gap for acknowledgement, with no
  void or cancellation log checked (Q1 default).
- A struck-out register value with a replacement needs a person to confirm, never an automatic
  guess (Q3 default).
- An ERP entry keyed after month end by a different user is flagged as a control exception (Q5
  default).

## How it fails and how someone would know

Ledgerline never sends a partial or wrong result, and never fails silently:

- A bad input file lists every problem and makes no model calls.
- OpenAI down or slow: 3 retries with backoff, then the step shows failed and ops gets an alert;
  "Retry this step" re-reads only that page, the rest stay cached.
- A failing completeness proof produces no report and no summary; ops gets an alert with the
  numbers.
- Telegram down: the run still finalises, the notification is marked failed, and a "Summary not
  delivered" banner offers Resend.
- Finalising twice, or a crash mid-send, never sends a second message.
- A paused free-tier Supabase project shows a red health check.

With `ALLOW_FAULTS=true` (never in production), `?fault=` with `EXTRACT_TIMEOUT`,
`EXTRACT_BAD_JSON`, `TELEGRAM_DOWN` or `PROOF_DROP_ROW` demonstrates each path on demand. Every
measured target is in `NFR_REPORT.md`.

## How the data was handled

Only Nocturnal's fictional pack is used; no real people, documents or numbers. Only the
register's photographed pages reach OpenAI — the ERP export and store sheet never leave
Supabase. Every normalised value keeps its raw value alongside it, with every change recorded in
the normalisation log. Photos and reports sit in private storage buckets, reachable only through
a signed link that expires (10 minutes to upload, 5 to download). The audit log cannot be edited
or deleted, even by the app. This demo keeps everything; production would agree a retention
period with the client.

## What I'd do with another week

- A Telegram/WhatsApp photo that starts a run by itself, via the WhatsApp Cloud API.
- The system learning new supplier and unit aliases from review decisions, each person-approved.
- Cell-level crops from the model's own bounding boxes, once their accuracy is measured.
- A month-over-month view of which source is most often the outlier.
- Writing approved corrections back to the ERP.
- A background queue instead of one request per page.
- A real retention policy and per-client data separation.

## AI tools used and for what

- **Claude** drafted the blueprint and reviewed the sample data before any code existed.
- **Claude Code** implemented every phase from its own spec, plan and task files (spec-driven:
  spec, then plan, then tasks, then tests first, then code).
- **GPT-5.6 Luna**, inside the product itself, reads each register page twice independently; it
  never sees ERP or store-sheet data.
