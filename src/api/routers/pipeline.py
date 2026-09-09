"""Pipeline stages: ingest -> check -> solve -> publish, plus run listing
(SPEC.md §4). Each endpoint is a thin translation of the matching CLI
command; the heavy lifting stays in `iff_scheduler` and `api.services`.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import pandas as pd
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from api.cli_helpers import conflicts_frame, load_assignments, load_clean_applicants
from api.dependencies import (
    ensure_run_exists,
    get_settings,
    resolve_run_dir,
    resolve_workspace,
    run_dir_if_present,
    service_account_file,
    workspace_pk,
)
from api.services import execute_solve, read_run_metrics, run_capacity_check
from iff_scheduler import workspace as ws
from iff_scheduler.db import supabase_enabled
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
from iff_scheduler.ingest.validate import (
    append_outputs,
    is_recoverable,
    run_ingest,
    write_outputs,
)
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


# `row_number`s a human chose to force past a recoverable rejection, kept next
# to the interim outputs so every re-ingest of the same CSV re-applies them.
_RECOVERED_ROWS_FILE = "recovered_rows.json"


def _recovered_rows_path(workspace_id: str):  # type: ignore[no-untyped-def]
    return ws.interim_dir(workspace_id) / _RECOVERED_ROWS_FILE


def _load_recovered_rows(workspace_id: str) -> set[int]:
    path = _recovered_rows_path(workspace_id)
    if not path.exists():
        return set()
    try:
        return {int(x) for x in json.loads(path.read_text(encoding="utf-8"))}
    except (ValueError, json.JSONDecodeError):
        return set()


def _save_recovered_rows(workspace_id: str, rows: set[int]) -> None:
    path = _recovered_rows_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(rows)), encoding="utf-8")


def _rejected_from_report(report_path) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Rejected rows of `validation_report.csv`, shaped for the UI's Rejected
    tab. Tolerant of an older report written before `csv_row` / the
    sub-division columns existed."""
    if not report_path.exists():
        return []
    df = pd.read_csv(report_path, dtype=str, keep_default_na=False)
    out: list[dict[str, Any]] = []
    for r in df.to_dict(orient="records"):
        if r.get("outcome") != "REJECTED":
            continue
        row_number = int(r["row_number"])
        code = r.get("reason_code", "")
        out.append(
            {
                "row_number": row_number,
                "csv_row": int(r["csv_row"]) if r.get("csv_row") else row_number + 1,
                "full_name": r.get("full_name", ""),
                "email": r.get("email", ""),
                "sub_division_1": r.get("sub_division_1", ""),
                "sub_division_2": r.get("sub_division_2", ""),
                "reason_code": code,
                "message": r.get("message", ""),
                "recoverable": is_recoverable(code),
            }
        )
    return out


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

        # A fresh import starts with a clean slate — any rows recovered against
        # the previous upload no longer apply.
        _recovered_rows_path(workspace_id).unlink(missing_ok=True)

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
    sa_file = service_account_file()

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


@router.get("/ingest-status")
def ingest_status(workspace_id: str) -> dict[str, Any]:
    """Whether this workspace has an ingested applicant list yet.

    The UI gates Check Capacity / Schedule! on this. Both endpoints 404 with
    "Run ingest first" when `applicants.clean.csv` is absent (as on a
    freshly created workspace), and firing them on click only to surface
    that error stacks error toasts for no reason. Cheap — reads one CSV's
    row count and nothing else.
    """
    resolve_workspace(workspace_id)
    path = ws.applicants_clean_path(workspace_id)
    if not path.exists():
        return {"ingested": False, "applicants": 0}
    try:
        count = int(len(pd.read_csv(path)))
    except (pd.errors.EmptyDataError, OSError):
        count = 0
    return {"ingested": count > 0, "applicants": count}


@router.get("/rejected")
def list_rejected(workspace_id: str) -> list[dict[str, Any]]:
    """The rejected rows of the latest validation report, for the workspace
    page's Rejected tab. `recoverable` says whether the "Recover" action
    applies (a missing/invalid email or an unmappable sub-division cannot be
    waved through)."""
    resolve_workspace(workspace_id)
    return _rejected_from_report(ws.validation_report_path(workspace_id))


@router.post("/recover/{row_number}")
def recover(workspace_id: str, row_number: int, settings: SettingsDep) -> dict[str, Any]:
    """Force a recoverable rejected row into the clean applicant list and
    re-run ingest over the stored CSV upload. The committee still has to
    re-run Schedule! for the recovered applicant to be placed."""
    resolve_workspace(workspace_id)

    raw_path = ws.raw_dir(workspace_id) / "upload.csv"
    if not raw_path.exists():
        raise HTTPException(
            status_code=409,
            detail="Recover is only available for CSV imports. Re-import the CSV, then recover.",
        )

    report_path = ws.validation_report_path(workspace_id)
    match = next(
        (r for r in _rejected_from_report(report_path) if r["row_number"] == row_number),
        None,
    )
    if match is None:
        raise HTTPException(
            status_code=404, detail=f"Row {row_number} is not in the rejected list."
        )
    if not match["recoverable"]:
        raise HTTPException(
            status_code=409,
            detail=f"Row {row_number} cannot be recovered ({match['reason_code']}).",
        )

    recovered = _load_recovered_rows(workspace_id)
    recovered.add(row_number)

    grid = build_slot_grid(settings.event)
    interim = ws.interim_dir(workspace_id)
    result = run_ingest(
        source=CsvApplicantSource(path=raw_path),
        event=settings.event,
        divisions=settings.divisions,
        grid=grid,
        force_accept_rows=frozenset(recovered),
    )
    write_outputs(
        result,
        clean_path=interim / "applicants.clean.csv",
        report_path=report_path,
    )
    _save_recovered_rows(workspace_id, recovered)

    summary = _ingest_summary(result.applicants, result.report)
    summary["recovered_row"] = row_number
    summary["message"] = "Applicant recovered. Re-run Schedule! to include them."
    return summary


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
    panels = resolve_panels(settings.panels, settings.rooms, grid)
    rooms = resolve_rooms(settings.rooms, grid)
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


_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/runs/{run_id}/xlsx")
def download_xlsx(workspace_id: str, run_id: str) -> FileResponse:
    """Download the `schedule.xlsx` a prior `publish` wrote for this run.

    The frontend's Google Sheets export needs a Google Workspace Shared
    Drive (a bare service account has no Drive storage of its own —
    docs/DEPLOY.md, "Google Drive export storage") and most committees won't
    have one, so this is the plain download path: `schedule.xlsx` is written
    to local disk by `publish` (which `Schedule!` already calls
    automatically), and this endpoint just serves that file.

    404s if the run itself doesn't exist (checked against whichever store is
    live); 409s if the run exists but was never published, or its published
    output isn't on *this* machine's disk — publish artefacts are a local
    file, never mirrored to Postgres, so a run solved before a Railway
    redeploy (an ephemeral filesystem — see docs/DEPLOY.md, "Persist run
    artefacts") needs Publish run again rather than a fresh Schedule!.
    """
    resolve_workspace(workspace_id)
    resolved_run_id = ensure_run_exists(workspace_id, run_id)
    xlsx_path = ws.output_dir(workspace_id) / resolved_run_id / "schedule.xlsx"
    if not xlsx_path.is_file():
        raise HTTPException(
            status_code=409,
            detail=(
                f"No published schedule.xlsx for run '{resolved_run_id}'. "
                "Publish this run (Schedule! does this automatically) and try again."
            ),
        )
    return FileResponse(
        xlsx_path,
        media_type=_XLSX_MEDIA_TYPE,
        filename=f"{workspace_id} schedule {resolved_run_id}.xlsx",
    )


@router.get("/runs")
def list_runs(workspace_id: str) -> list[dict[str, Any]]:
    resolve_workspace(workspace_id)
    if supabase_enabled():
        from iff_scheduler.db import run_repo

        # `has_assignments` comes from the run's own metrics rather than being
        # hard-coded True: a run that solved but persisted nothing would
        # otherwise be offered in the history and then open empty.
        return [
            {
                "run_id": row["run_label"],
                "has_assignments": int((row.get("metrics") or {}).get("interviews_placed", 0)) > 0,
                "created_at": row.get("created_at") or row["run_label"],
            }
            for row in run_repo.list_runs(workspace_pk(workspace_id))
        ]
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
    if supabase_enabled():
        from iff_scheduler.db import assignment_repo, run_repo

        row = run_repo.get_run(workspace_pk(workspace_id), run_id)
        if row is None:
            # A run written while the DB was unreachable still has its
            # directory on disk; fall back to it rather than 404ing on a run
            # the history list happily shows.
            local = run_dir_if_present(workspace_id, run_id)
            if local is not None:
                return read_run_metrics(local)
            raise HTTPException(
                status_code=404,
                detail=f"Run '{run_id}' not found for workspace '{workspace_id}'.",
            )
        return {
            "run_id": row["run_label"],
            "status": row.get("status"),
            "metrics": row.get("metrics") or {},
            "interviews_placed": len(assignment_repo.list_assignments(row["id"])),
        }
    run_dir = resolve_run_dir(workspace_id, run_id)
    return read_run_metrics(run_dir)
