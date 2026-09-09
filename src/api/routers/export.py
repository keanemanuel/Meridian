"""Google Sheets export of a finished run (SPEC.md §9, FR-50).

Builds the timetable the committee runs the evenings off: one tab per event
day, one section per panel, one row per slot, with "Arrived?" / "Interview?"
tick columns. The layout itself lives in
`iff_scheduler.export.sheets_writer` and is pure; this router only resolves
the run's assignments (Supabase or the run directory) and hands over an
authorised gspread client.
"""

from __future__ import annotations

import re
import sys
import traceback
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.cli_helpers import load_assignments
from api.dependencies import (
    DRIVE_FOLDER_VAR,
    drive_folder_id,
    get_settings,
    resolve_run_dir,
    resolve_run_pk,
    resolve_workspace,
    service_account_file,
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


# Google answers "API has not been used in project N before or it is disabled"
# with a console URL buried in a paragraph. Creating the spreadsheet needs the
# Drive API and sharing it needs Drive too, so a project with only the Sheets
# API enabled ingests fine and then fails here — which is exactly the shape of
# "the export silently does nothing".
_API_DISABLED = re.compile(
    r"(?P<api>[\w ]+ API) has not been used in project (?P<project>\d+) before or it is disabled"
)


def _google_failure(exc: Exception, *, folder_configured: bool) -> tuple[int, str]:
    """(status, detail) the committee can act on, from whatever gspread raised.

    A recognised misconfiguration answers 409, like the credential errors
    above: it is a settings problem that will not fix itself, and 502/503 is
    what the client treats as a transient cold start worth retrying. Only an
    unclassified upstream failure keeps 502.
    """
    text = str(exc)

    disabled = _API_DISABLED.search(text)
    if disabled:
        api, project = disabled.group("api").strip(), disabled.group("project")
        return 409, (
            f"The {api} is not enabled on the service account's Google Cloud project "
            f"({project}), so the timetable Sheet could not be created. Enable it at "
            f"https://console.cloud.google.com/apis/library?project={project} and try "
            "again in a minute. The export needs both the Google Sheets API and the "
            "Google Drive API: Sheets to write the timetable, Drive to create the file "
            "and share it by link."
        )

    if "storageQuotaExceeded" in text:
        # A bare service account has *no* personal Drive storage at all — this
        # is a platform limit, not a quota that filled up, so it never
        # self-resolves and "try again later" is the wrong advice. The one
        # storage a service account can write to is a Shared Drive it has been
        # added to; an ordinary "My Drive" folder shared as Editor does not
        # work, because Drive bills storage to whoever creates the file, not
        # to the folder's owner. See docs/DEPLOY.md, "Google Drive export
        # storage".
        if folder_configured:
            return 409, (
                f"Google still refused the export as an out-of-storage error even though "
                f"{DRIVE_FOLDER_VAR} is set. That folder must be inside a *Shared Drive* "
                "with the service account added as a member (Content Manager or above) — "
                "an ordinary My Drive folder shared as Editor does not give the service "
                "account any storage, because Drive attributes a file's storage to whoever "
                "created it, not to the parent folder's owner. See docs/DEPLOY.md, "
                f'"Google Drive export storage". ({text[:200]})'
            )
        return 409, (
            "Google refused the export because the service account has no Drive storage "
            "of its own — this never resolves on its own. Set "
            f"{DRIVE_FOLDER_VAR} to a folder inside a Shared Drive that the service "
            "account has been added to as a member, and redeploy. See docs/DEPLOY.md, "
            f'"Google Drive export storage". ({text[:200]})'
        )

    if "invalid_grant" in text or "unauthorized_client" in text:
        return 409, (
            "Google rejected the service account key. It may have been revoked, or "
            f"the server clock may be wrong. ({text[:200]})"
        )

    return 502, (
        f"Google rejected the export: {type(exc).__name__}: {text[:300]} "
        "Check that the Sheets and Drive APIs are enabled for the service account's "
        "project and that its key is still valid."
    )


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

    # Order matters. The run lookup is local and cheap, and "that run does not
    # exist" is a more specific answer than "your credentials are wrong", so it
    # goes first. Credentials are then resolved before the first Google call,
    # so a bad key is reported as a bad key rather than as whatever json.load
    # happens to say about a file the caller never named.
    assignments = _load_run_assignments(workspace_id, run_id)
    if not assignments:
        raise HTTPException(
            status_code=409,
            detail=f"Run '{run_id}' has no assignments to export — solve first.",
        )
    key_file = service_account_file()

    grid = build_slot_grid(settings.event)
    panels = resolve_panels(settings.panels, settings.rooms, grid)
    rooms = resolve_rooms(settings.rooms, grid)
    views = build_room_views(assignments, panels, rooms, grid.slots)
    tabs = build_tabs(views, settings.event.timezone)

    title = body.title or f"{settings.event.event_name} · {workspace_id} · {run_id}"
    folder_id = drive_folder_id()
    try:
        client = open_export_client(key_file)
        exported = export_timetable(
            client,
            title,
            tabs,
            settings.event.timezone,
            share_with_link=body.share_with_link,
            folder_id=folder_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        # Everything past this point is Google's side of the call: the Drive
        # or Sheets API disabled on the project, the service account out of
        # storage quota, a revoked key. The traceback goes to stderr (the
        # platform captures it) and the client gets one line naming the stage
        # that failed, rather than a bare 500.
        print(
            f"POST export/sheets failed for workspace={workspace_id!r} run={run_id!r} "
            f"while talking to Google.",
            file=sys.stderr,
        )
        traceback.print_exc()
        status, detail = _google_failure(exc, folder_configured=folder_id is not None)
        raise HTTPException(status_code=status, detail=detail) from exc

    return {
        "sheet_url": exported.sheet_url,
        "sheet_id": exported.sheet_id,
        "tabs": exported.tabs,
        "rows_written": exported.rows_written,
        "clashes": exported.clashes,
        "folder_id": folder_id,
    }
