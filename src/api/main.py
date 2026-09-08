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
# directly into GOOGLE_SERVICE_ACCOUNT_FILE / GMAIL_OAUTH_CREDENTIALS /
# GMAIL_TOKEN_CACHE. The core reads those as paths, so materialize any
# JSON-shaped value to a file under /tmp before anything else runs (docs/DEPLOY.md).
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
    FastAPI uses for HTTPException, rather than a bare 500."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/api/health", tags=["meta"])
def health() -> dict[str, Any]:
    return {"status": "ok"}


app.include_router(workspaces.router)
app.include_router(pipeline.router)
app.include_router(schedule.router)
app.include_router(notify.router)
