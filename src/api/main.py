"""Meridian FastAPI backend (beta, SPEC.md §14).

A thin wrapper over the alpha core. Every route calls the same functions the
CLI calls; `src/iff_scheduler/` is never imported for anything other than
those public functions and is never modified.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routers import notify, pipeline, schedule, workspaces

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
