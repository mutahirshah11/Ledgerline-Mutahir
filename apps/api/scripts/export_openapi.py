"""Write `apps/api/openapi.json` from the FastAPI app, with no server running (005/006 web, T006).

The web app generates its typed client (`apps/web/lib/api-types.ts`) from this file via
`openapi-typescript` (`pnpm gen:api`, research R5); CI fails if regenerating changes it (T009,
`make gen-api`). The schema is built from `app.main:app` directly (`app.openapi()`), so no
`uvicorn` process, network call or live credentials are needed; default `Settings` values are
enough for route registration.

Usage (from apps/api)::

    uv run python -m scripts.export_openapi
"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import app

SCRIPTS_DIR = Path(__file__).resolve().parent
API_DIR = SCRIPTS_DIR.parent
OUTPUT_PATH = API_DIR / "openapi.json"


def export_openapi(output_path: Path = OUTPUT_PATH) -> Path:
    """Write the app's OpenAPI schema as pretty JSON to `output_path`; return that path."""
    schema = app.openapi()
    output_path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def main() -> None:
    path = export_openapi()
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
