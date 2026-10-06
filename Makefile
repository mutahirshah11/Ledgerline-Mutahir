# Ledgerline task runner. Windows users: run `make` from Git Bash.

API := apps/api
WEB := apps/web

.PHONY: setup dev test lint seed eval

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
