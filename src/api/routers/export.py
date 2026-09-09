"""Google Sheets export of a finished run (SPEC.md §9, FR-50).

Builds the timetable the committee runs the evenings off: one tab per event
day, one section per panel, one row per slot, with "Arrived?" / "Interview?"
tick columns. The layout itself lives in
`iff_scheduler.export.sheets_writer` and is pure; this router only resolves
the run's assignments (Supabase or the run directory) and hands over an
authorised gspread client.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.cli_helpers import load_assignments
from api.dependencies import (
    get_settings,
    resolve_run_dir,
    resolve_run_pk,
    resolve_workspace,
)
from iff_scheduler.db import supabase_enabled
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.domain.models import Assignment
from iff_scheduler.export.room_view import build_room_views
from iff_scheduler.export.sheets_writer import (
    build_tabs,
    export_timetable,
    open_export_client,
)
from iff_scheduler.scheduling.base import resolve_panels, resolve_rooms
from iff_scheduler.settings import Settings

router = APIRouter(prefix="/api/workspaces/{workspace_id}/runs/{run_id}/export", tags=["export"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


class SheetExportBody(BaseModel):
    """`title` overrides the generated spreadsheet name; `share_with_link`
    can be turned off for a committee that shares by hand instead."""

    title: str | None = None
    share_with_link: bool = True


def _load_run_assignments(workspace_id: str, run_id: str) -> list[Assignment]:
    """The run's assignments from whichever store is live.

    Supabase is authoritative when configured; the run directory is the
    fallback and, in file-store mode, the only source. Deliberately does not
    require a run *directory* in DB mode: a run solved on another Railway
    instance has its rows in Postgres but no local directory, and refusing
    the export there would be a phantom 404.
    """
    if supabase_enabled():
        from iff_scheduler.db import assignment_repo

        assignments = assignment_repo.list_assignments(resolve_run_pk(workspace_id, run_id))
        if assignments:
            return assignments

    run_dir = resolve_run_dir(workspace_id, run_id)
    path = run_dir / "assignments.csv"
    if not path.exists():
        raise HTTPException(status_code=409, detail=f"{path} not found — solve first.")
    return load_assignments(path)


@router.post("/sheets")
def export_to_sheets(
    workspace_id: str,
    run_id: str,
    settings: SettingsDep,
    body: SheetExportBody | None = None,
) -> dict[str, Any]:
    """Create a new Google Sheet holding this run's timetable and return its
    URL. Nothing is written back into the workspace's linked response Sheet."""
    resolve_workspace(workspace_id)
    body = body or SheetExportBody()

    load_dotenv()
    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not service_account_file:
        raise HTTPException(
            status_code=409,
            detail="GOOGLE_SERVICE_ACCOUNT_FILE is not set in the environment.",
        )

    assignments = _load_run_assignments(workspace_id, run_id)
    if not assignments:
        raise HTTPException(
            status_code=409,
            detail=f"Run '{run_id}' has no assignments to export — solve first.",
        )

    grid = build_slot_grid(settings.event)
    panels = resolve_panels(settings.panels, settings.rooms, grid)
    rooms = resolve_rooms(settings.rooms, grid)
    views = build_room_views(assignments, panels, rooms, grid.slots)
    tabs = build_tabs(views, settings.event.timezone)

    title = body.title or f"{settings.event.event_name} — {workspace_id} — {run_id}"
    client = open_export_client(service_account_file)
    exported = export_timetable(
        client,
        title,
        tabs,
        settings.event.timezone,
        share_with_link=body.share_with_link,
    )

    return {
        "sheet_url": exported.sheet_url,
        "sheet_id": exported.sheet_id,
        "tabs": exported.tabs,
        "rows_written": exported.rows_written,
        "clashes": exported.clashes,
    }
