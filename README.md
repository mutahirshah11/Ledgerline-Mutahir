# Ledgerline · Three-way receiving reconciliation (Task 4)
[![CI](https://github.com/mutahirshah11/Ledgerline-Mutahir/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mutahirshah11/Ledgerline-Mutahir/actions/workflows/ci.yml)

## What it does

Kestrel's handwritten register, ERP GRNs and storekeeper sheet never agree, and month-end takes
three days. Upload the four files once: an AI model reads the two register photos (twice,
independently), then code — never the model — normalises, reconciles, runs control checks and
proves completeness. Agreeing lines clear themselves; only doubtful items reach a person. On
finalise the controller gets a short Telegram summary plus `report.html`, exceptions first.

On the pack: 86 rows (28 register, 28 ERP, 30 sheet), every one in exactly one outcome. Problems
found and how they surface:

| GRN | Problem | Outcome |
| --- | --- | --- |
| 4409 | Sheet keyed twice | Duplicate, not double-counted |
| 4412 | ERP lot L2609-021 vs L2609-012 | Mismatch, ERP is the odd one out |
| 4415 | ERP date 13 Sep vs 12 Sep (a Sunday) | Mismatch + flag |
| 4417 | ERP qty 46 CTN vs 48 | Mismatch |
| 4419 | Register 60 struck out, 66 written | Person confirms, then sheet mismatch |
| 4421 | In register and sheet, not ERP | Missing from ERP |
| 4425 | Register 1200 yds = 1097.28 m; sheet 1200 m | Converted; sheet mismatch |
| 4428 | Register qty under an ink blot | Person enters it; never guessed |
| 4429 | In no source | Sequence gap to acknowledge |
| 4430 | ERP only, keyed 2 Oct by `admin2` | Missing + backdated + unusual user |
| — | Sheet return row (Sunday, out of order) | Non-receipt |

Not flagged: "H202" (OCR of H2O2), text-date and Excel-serial dates, supplier and unit aliases.

## How to run

**Hosted:** https://ledgerline-mutahir.vercel.app — demo credentials are in the submission
email. Start a run, upload the four files from `data/kestrel-2026-09/`, resolve the review items,
check Proof, then Finalise.

**Local** (Windows: Git Bash): Python 3.12, `uv`, Node 20+, `pnpm`, Supabase CLI.

1. `make setup`; create a Supabase project, `supabase link`, `supabase db push`.
2. Copy `apps/api/.env.example` to `apps/api/.env`, `apps/web/.env.example` to
   `apps/web/.env.local`, fill in the table below.
3. `make seed BUILDER_EMAIL=you@example.com` makes you `CONTROLLER`; set a password in Supabase.
4. `make dev`, open `http://localhost:3000`. `make test` runs the offline suite (no keys).

| Variable | App | Purpose |
| --- | --- | --- |
| `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (secret) | api | Database and private storage |
| `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY` | web | Login only; RLS denies tables |
| `OPENAI_API_KEY` (secret), `EXTRACT_MODEL`, `PROMPT_VERSION` | api | Register reads |
| `EXTRACT_PRICE_INPUT_PER_MTOK`, `EXTRACT_PRICE_OUTPUT_PER_MTOK`, `EXTRACT_EST_INPUT_TOKENS_PER_PAGE`, `EXTRACT_EST_OUTPUT_TOKENS_PER_PAGE` | api | Read-cost estimate |
| `TELEGRAM_BOT_TOKEN` (secret), `TELEGRAM_CONTROLLER_CHAT_ID`, `TELEGRAM_OPS_CHAT_ID` | api | Summary chat and ops-alert chat |
| `LEDGERLINE_NOW`, `LEDGERLINE_CLOCK_MODE`, `LEDGERLINE_NOW_ANCHOR` | api | Scenario clock (below) |
| `ALLOW_FAULTS` | api | `true` only for the fault demo |
| `APP_BASE_URL`, `API_BASE_URL`, `CORS_ORIGINS` | both | URLs |
| `BUILDER_EMAIL` | `make seed` | Your login |
| `OPENAI_TIMEOUT_S`, `EXTRACT_ATTEMPTS`, `EXTRACT_BACKOFF_S`, `TELEGRAM_ATTEMPTS`, `TELEGRAM_BACKOFF_S`, `TELEGRAM_TIMEOUT_S`, `UPLOAD_WINDOW_S`, `DOWNLOAD_URL_S`, `JWKS_CACHE_S`, `JWKS_MIN_REFETCH_S`, `REPO_BACKEND` | api | Tuning; defaults are right |

**Scenario clock:** `LEDGERLINE_NOW=2026-10-15T08:30:00+05:00` replays the brief's "now";
`LEDGERLINE_CLOCK_MODE=frozen` holds it, `offset` (with `LEDGERLINE_NOW_ANCHOR`) lets it tick.
All code reads time through `app/core/clock.py`.

**Telegram → WhatsApp:** the sender is one small class (`app/services/telegram.py`). Moving to
the WhatsApp Cloud API means a pre-approved template for the summary (business-initiated),
`report.html` as a document message, and phone numbers instead of chat IDs; retries and
at-most-once logic stay as they are.

## Assumptions

- The GRN number is the match key; no source is treated as truth — the odd one out is named and a
  person decides.
- An exact unit conversion (yards to metres) counts as a match, logged.
- Returns are non-receipts, not reconciled against debit notes.
- A GRN in no source is a sequence gap; no void log was supplied.
- Struck-out or illegible register values always go to a person.
- An ERP entry keyed after month end, or by an unusual user, is a control exception.

## How it fails and how someone would know

Nothing partial or wrong is ever sent, and nothing fails silently:

- Bad input file: every problem listed, no model call made.
- OpenAI slow or down: 3 retries with backoff, then the step fails and ops is alerted; retry
  re-reads only the failed page.
- Proof fails: no report, no summary, ops alerted with the numbers.
- Telegram down: the run still finalises, the summary is marked failed, a "Summary not delivered —
  Resend" banner appears, ops is alerted.
- Finalising twice or a crash mid-send never sends a second message.

With `ALLOW_FAULTS=true`, add `?fault=` with `EXTRACT_TIMEOUT`, `EXTRACT_BAD_JSON`,
`TELEGRAM_DOWN` or `PROOF_DROP_ROW` to show each path. Measured targets: `NFR_REPORT.md`.

## How the data was handled

Only Nocturnal's fictional pack. Only the register photos go to OpenAI; ERP and sheet data never
leave Supabase. Every normalised value keeps its raw value and a log entry. Files sit in private
buckets behind expiring signed links (10 min upload, 5 min download). The browser's anon key can
read no table; only the API's service key can. The audit log cannot be edited or deleted. No
secrets in the repo: every `.env` file is git-ignored and gitleaks runs in CI.

## What I'd do with another week

- Monthly runs that skip register pages already read (hash the photo, reuse its rows).
- WhatsApp Cloud API, including photographing pages straight into a run.
- Learn supplier and unit aliases from review decisions, each person-approved.
- Cell crops over the photo in the review queue.
- Write approved corrections back to the ERP; a background queue instead of one request per page.

## AI tools used and for what

- **Claude / Claude Code:** blueprint, spec-driven specs, plans and tasks, tests-first
  implementation, review.
- **Google Antigravity / Gemini:** implementation, test runs and environment setup.
- **GPT-5.6 Luna** (inside the product): reads each register page twice; never sees ERP or sheet
  data.
