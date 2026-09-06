"""Pipeline stages: ingest -> check -> solve -> publish, plus run listing
(SPEC.md §4). Each endpoint is a thin translation of the matching CLI
command; the heavy lifting stays in `iff_scheduler` and `api.services`.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from api.cli_helpers import conflicts_frame, load_assignments, load_clean_applicants
from api.dependencies import get_settings, resolve_run_dir, resolve_workspace
from api.services import execute_solve, read_run_metrics, run_capacity_check
from iff_scheduler import workspace as ws
from iff_scheduler.domain.enums import Severity
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.export.applicant_view import build_applicant_view
from iff_scheduler.export.html_writer import (
    write_applicant_view_html,
    write_panel_view_html,
    write_room_view_html,
)
from iff_scheduler.export.panel_view import build_panel_views
from iff_scheduler.export.room_view import build_room_views
from iff_scheduler.export.xlsx_writer import write_xlsx
from iff_scheduler.ingest.csv_source import CsvApplicantSource
from iff_scheduler.ingest.sheets_source import (
    SheetsApplicantSource,
    open_worksheet,
    run_incremental_sheets_ingest,
    write_watermark,
)
from iff_scheduler.ingest.validate import append_outputs, run_ingest, write_outputs
from iff_scheduler.scheduling.base import resolve_panels, resolve_rooms
from iff_scheduler.scheduling.postprocess import build_conflicts
from iff_scheduler.settings import Settings

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["pipeline"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _ingest_summary(applicants: list[Any], report: list[Any]) -> dict[str, Any]:
    return {
        "applicants": len(applicants),
        "rejected": sum(1 for r in report if r.outcome == "REJECTED"),
        "collapsed": sum(1 for r in report if r.outcome == "COLLAPSED"),
        "warnings": sum(1 for r in report if r.outcome == "WARNING"),
        "report": [r.model_dump(mode="json") for r in report],
    }


@router.post("/ingest")
async def ingest(
    workspace_id: str,
    settings: SettingsDep,
    source: Annotated[str, Form()] = "csv",
    file: Annotated[UploadFile | None, File()] = None,
    force: Annotated[bool, Form()] = False,
    worksheet: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Ingest + normalise + validate (FR-01..FR-07).

    `source=csv` takes a multipart file upload (a Google Form CSV export) and
    does a one-shot full read. `source=sheets` reads the workspace's linked
    Sheet incrementally, exactly as `iffsched ingest --source sheets` does.
    """
    resolve_workspace(workspace_id)
    grid = build_slot_grid(settings.event)
    interim = ws.interim_dir(workspace_id)
    interim.mkdir(parents=True, exist_ok=True)
    clean_path = interim / "applicants.clean.csv"
    report_path = interim / "validation_report.csv"

    if source == "csv":
        if file is None:
            raise HTTPException(status_code=422, detail="source=csv requires an uploaded file.")
        raw_dir = ws.raw_dir(workspace_id)
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / "upload.csv"
        raw_path.write_bytes(await file.read())

        result = run_ingest(
            source=CsvApplicantSource(path=raw_path),
            event=settings.event,
            divisions=settings.divisions,
            grid=grid,
        )
        write_outputs(result, clean_path=clean_path, report_path=report_path)
        return _ingest_summary(result.applicants, result.report)

    if source != "sheets":
        raise HTTPException(status_code=422, detail=f"Unknown source '{source}'. Use csv|sheets.")

    meta = resolve_workspace(workspace_id)
    if meta.sheet_id is None:
        raise HTTPException(
            status_code=409,
            detail=f"Workspace '{workspace_id}' has no Sheet attached (set-sheet first).",
        )
    load_dotenv()
    sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if not sa_file:
        raise HTTPException(
            status_code=409, detail="GOOGLE_SERVICE_ACCOUNT_FILE is not set in the environment."
        )

    worksheet_handle = open_worksheet(sa_file, meta.sheet_id, worksheet)
    sheets_source = SheetsApplicantSource(worksheet=worksheet_handle)
    watermark_path = ws.last_ingested_row_path(workspace_id)
    incremental = run_incremental_sheets_ingest(
        source=sheets_source,
        event=settings.event,
        divisions=settings.divisions,
        grid=grid,
        clean_path=clean_path,
        watermark_path=watermark_path,
        force=force,
    )
    if incremental.new_row_count == 0:
        return {
            "applicants": 0,
            "rejected": 0,
            "collapsed": 0,
            "warnings": 0,
            "report": [],
            "new_rows": 0,
        }
    if force:
        write_outputs(incremental.result, clean_path=clean_path, report_path=report_path)
    else:
        append_outputs(incremental.result, clean_path=clean_path, report_path=report_path)
    write_watermark(watermark_path, incremental.watermark_after)
    summary = _ingest_summary(incremental.result.applicants, incremental.result.report)
    summary["new_rows"] = incremental.new_row_count
    return summary


@router.post("/check")
def check(workspace_id: str, settings: SettingsDep) -> dict[str, Any]:
    resolve_workspace(workspace_id)
    return run_capacity_check(settings, workspace_id)


class SolveBody(BaseModel):
    skip_check: bool = False


@router.post("/solve")
def solve(
    workspace_id: str, settings: SettingsDep, body: SolveBody | None = None
) -> dict[str, Any]:
    resolve_workspace(workspace_id)
    body = body or SolveBody()
    return execute_solve(settings, workspace_id, skip_check=body.skip_check)


class PublishBody(BaseModel):
    run: str = "latest"
    formats: list[str] = ["xlsx", "html"]


@router.post("/publish")
def publish(
    workspace_id: str, settings: SettingsDep, body: PublishBody | None = None
) -> dict[str, Any]:
    body = body or PublishBody()
    run_dir = resolve_run_dir(workspace_id, body.run)
    assignments_path = run_dir / "assignments.csv"
    if not assignments_path.exists():
        raise HTTPException(status_code=409, detail=f"{assignments_path} not found — solve first.")

    wanted = {f.strip().lower() for f in body.formats if f.strip()}
    unknown = wanted - {"xlsx", "html"}
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown format(s): {sorted(unknown)}.")

    applicants_path = ws.applicants_clean_path(workspace_id)
    if not applicants_path.exists():
        raise HTTPException(status_code=404, detail=f"No applicants at {applicants_path}.")

    grid = build_slot_grid(settings.event)
    applicants = load_clean_applicants(applicants_path)
    panels = resolve_panels(settings.panels, grid)
    rooms = resolve_rooms(settings.rooms)
    assignments = load_assignments(assignments_path)

    resolved_run_id = run_dir.resolve().name
    publish_dir = ws.output_dir(workspace_id) / resolved_run_id
    publish_dir.mkdir(parents=True, exist_ok=True)

    conflicts = build_conflicts(assignments, applicants, panels)
    conflicts_frame(conflicts).to_csv(publish_dir / "conflicts.csv", index=False)

    room_views = build_room_views(assignments, panels, rooms, grid.slots)
    applicant_rows = build_applicant_view(assignments)
    panel_views = build_panel_views(assignments, panels, grid.slots)

    if "xlsx" in wanted:
        write_xlsx(
            publish_dir / "schedule.xlsx", room_views, applicant_rows, panel_views, conflicts
        )
    if "html" in wanted:
        html_dir = publish_dir / "html"
        write_room_view_html(room_views, html_dir)
        write_applicant_view_html(applicant_rows, html_dir)
        write_panel_view_html(panel_views, html_dir)

    return {
        "run_id": resolved_run_id,
        "output_dir": str(publish_dir),
        "room_views": len(room_views),
        "applicants": len(applicant_rows),
        "panels": len(panel_views),
        "clashes_red": sum(1 for c in conflicts if c.severity == Severity.RED),
        "warnings_amber": sum(1 for c in conflicts if c.severity == Severity.AMBER),
        "formats": sorted(wanted),
    }


@router.get("/runs")
def list_runs(workspace_id: str) -> list[dict[str, Any]]:
    resolve_workspace(workspace_id)
    runs_dir = ws.runs_dir(workspace_id)
    if not runs_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        out.append(
            {
                "run_id": entry.name,
                "has_assignments": (entry / "assignments.csv").exists(),
                "created_at": entry.name,
            }
        )
    return out


@router.get("/runs/{run_id}")
def get_run(workspace_id: str, run_id: str) -> dict[str, Any]:
    run_dir = resolve_run_dir(workspace_id, run_id)
    return read_run_metrics(run_dir)
