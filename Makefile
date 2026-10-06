# Ledgerline task runner. Windows users: run `make` from Git Bash.

API := apps/api
WEB := apps/web

.PHONY: setup dev test lint seed

setup:
	cd $(API) && uv sync
	cd $(WEB) && pnpm install --frozen-lockfile
	uv run --project $(API) pre-commit install

dev:
	cd $(API) && uv run uvicorn app.main:app --reload --port 8000 & \
	cd $(WEB) && pnpm dev

# Must match the CI commands in .github/workflows/ci.yml
test:
	cd $(API) && uv run pytest -m "not integration"

lint:
	cd $(API) && uv run ruff check
	cd $(API) && uv run ruff format --check
	cd $(API) && uv run mypy app scripts tests
	cd $(WEB) && pnpm lint
	cd $(WEB) && pnpm exec tsc --noEmit

seed:
	cd $(API) && uv run python -m scripts.seed_users $(BUILDER_EMAIL)
