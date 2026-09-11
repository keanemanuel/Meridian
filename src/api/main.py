"""Meridian FastAPI backend (beta, SPEC.md §14).

A thin wrapper over the alpha core. Every route calls the same functions the
CLI calls; `src/iff_scheduler/` is never imported for anything other than
those public functions and is never modified.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# `api` and `iff_scheduler` are sibling top-level packages under `src/` (see
# `[tool.hatch.build.targets.wheel] packages` in pyproject.toml). Locally that
# layer is put on sys.path by `pip install -e .` or `uvicorn --app-dir src`.
# Vercel's Python runtime imports this file directly with no such wrapper, so
# without this the `api.routers` import below — and every `iff_scheduler`
# import behind it — fails at cold start. Safe to keep everywhere: a no-op
# once `src/` is already on the path.
_SRC_DIR = str(Path(__file__).resolve().parent.parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# Vercel's dashboard has no file upload — a deploy pastes credential JSON
# directly into GMAIL_OAUTH_CREDENTIALS / GMAIL_TOKEN_CACHE. The core reads
# those as paths, so materialize any JSON-shaped value to a file under /tmp
# before anything else runs (docs/DEPLOY.md).
from api.credentials_bootstrap import materialize_json_credentials  # noqa: E402

materialize_json_credentials()

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from api.routers import notify, pipeline, schedule, workspaces  # noqa: E402

app = FastAPI(
    title="Meridian API",
    description="Interview scheduler for IFF recruitment — HTTP wrapper over the alpha core.",
    version="0.1.0",
)

# Internal committee tool — no browser-facing origin restrictions (SPEC.md §6.1).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ValueError)
async def value_error_handler(_request: Request, exc: ValueError) -> JSONResponse:
    """Core code fails loudly with ValueError on bad input (CLAUDE.md
    invariant 3). Surface that as a 400 with the same {detail: ...} shape
    FastAPI uses for HTTPException, rather than a bare 500.

    `json.JSONDecodeError` is a ValueError too, but it is never the caller's
    fault — it means a file *on the server* (a credentials key, a config
    snapshot) is malformed. Answering 400 with its bare message produced
    things like "Extra data: line 14 column 1 (char 2364)", which says
    nothing about which file or which env var. Those get a 500, a stderr
    traceback the platform can capture, and a message that at least names
    the category.
    """
    import json
    import sys
    import traceback

    if isinstance(exc, json.JSONDecodeError):
        print("Unhandled JSONDecodeError — a server-side JSON file is malformed:", file=sys.stderr)
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={
                "detail": (
                    f"A JSON file on the server could not be parsed: {exc.msg} "
                    f"(line {exc.lineno}, column {exc.colno}). This is a credentials "
                    "or configuration problem, not a problem with the request. "
                    "Check the service logs for which file."
                )
            },
        )
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/api/debug", tags=["meta"])
def debug() -> dict[str, Any]:
    """Deploy diagnostics: which store the API resolved at runtime and
    whether the raw `SUPABASE_*` env vars are visible to the process.
    Reports booleans only — never the values — so it is safe to expose."""
    import os

    from dotenv import load_dotenv

    from iff_scheduler import workspace as ws
    from iff_scheduler.db import supabase_enabled

    load_dotenv()
    return {
        "supabase_enabled": supabase_enabled(),
        "supabase_url_set": bool(os.environ.get("SUPABASE_URL")),
        "supabase_key_set": bool(os.environ.get("SUPABASE_KEY")),
        "workspaces_file_exists": ws.workspaces_file().exists(),
    }


app.include_router(workspaces.router)
app.include_router(pipeline.router)
app.include_router(schedule.router)
app.include_router(notify.router)
