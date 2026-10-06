# Ledgerline task runner. Windows users: run `make` from Git Bash.

API := apps/api
WEB := apps/web

.PHONY: setup dev test lint seed

setup:
	cd $(API) && uv sync
	cd $(WEB) && pnpm install --frozen-lockfile
	cd $(API) && uv run pre-commit install --config ../../.pre-commit-config.yaml

dev:
	cd $(API) && uv run uvicorn app.main:app --reload --port 8000 & \
	cd $(WEB) && pnpm dev

# Must match the CI commands in .github/workflows/ci.yml
test:
	cd $(API) && uv run pytest -m "not integration"

lint:
	cd $(API) && uv run ruff check
	cd $(API) && uv run mypy app
	cd $(WEB) && pnpm lint
	cd $(WEB) && pnpm exec tsc --noEmit

seed:
	@echo "seed: available after T053 (allowlist seed)"
