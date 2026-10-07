# Ledgerline task runner. Windows users: run `make` from Git Bash.

API := apps/api
WEB := apps/web

.PHONY: setup dev test lint seed eval gen-api test-web smoke smoke-reset

setup:
	cd $(API) && uv sync
	cd $(WEB) && pnpm install --frozen-lockfile
	uv run --project $(API) pre-commit install

dev:
	cd $(API) && uv run uvicorn app.main:app --reload --port 8000 & \
	cd $(WEB) && pnpm dev

# Same checks as CI (.github/workflows/ci.yml). lint also runs tsc, which CI covers via pnpm build.
test:
	cd $(API) && uv run pytest -m "not integration and not live"

lint:
	cd $(API) && uv run ruff check
	cd $(API) && uv run ruff format --check
	cd $(API) && uv run mypy app scripts tests
	cd $(WEB) && pnpm lint
	cd $(WEB) && pnpm exec tsc --noEmit

seed:
	cd $(API) && uv run python -m scripts.seed_users $(BUILDER_EMAIL)

# Extraction eval (003 research R11): offline on the committed reads by default.
# Pass flags with ARGS, e.g. make eval ARGS="--live --pages 1 --price-in 1.25 --price-out 10".
eval:
	cd $(API) && uv run python -m scripts.eval_extraction $(ARGS)

# Regenerate the typed API client from the FastAPI app (006-web T006, research R5). CI fails if
# this changes apps/web/lib/api-types.ts (gen-api freshness step, T009).
gen-api:
	cd $(API) && uv run python -m scripts.export_openapi
	cd $(WEB) && pnpm gen:api

# Playwright "unit" + "screens" projects against a mocked API (006-web T010).
test-web:
	cd $(WEB) && pnpm test:e2e

# Playwright "smoke" project against a deployed Preview URL (006-web T010, R15). Never run
# against Production (tasks.md "Notes").
smoke:
	cd $(WEB) && pnpm test:smoke

# Resets the LINKED Supabase project's data (migrations + seed) before a smoke run (006-web T010,
# R15). Test project only — never point this at Production. Confirms before running.
smoke-reset:
	@ref=$$(cat supabase/.temp/project-ref 2>/dev/null || echo "(no linked project found)"); \
	echo "This will run 'supabase db reset --linked' against project: $$ref"; \
	read -p "Type the project ref to confirm, or anything else to abort: " confirm; \
	if [ "$$confirm" = "$$ref" ] && [ -n "$$ref" ]; then \
		supabase db reset --linked; \
	else \
		echo "Aborted: confirmation did not match linked project ref."; \
		exit 1; \
	fi
