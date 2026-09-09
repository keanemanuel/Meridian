"""Assignment viewing, manual single-assignment edits, and re-solve
(FR-40..FR-42, SPEC.md §12).

A manual edit here does exactly what `iffsched lock` + `iffsched solve` do:
the whole edited schedule is run through `validate_edits` (E-12) and, only
if it is legal, the touched choice is written to
`locks/pinned_assignments.csv` so every subsequent solve honours it (C6).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.cli_helpers import (
    assignments_frame,
    load_assignments,
    load_clean_applicants,
    load_locks,
    write_locks,
)
from api.dependencies import (
    ensure_run_exists,
    get_settings,
    resolve_run_dir,
    resolve_run_pk,
    run_dir_if_present,
)
from api.services import execute_solve
from iff_scheduler import workspace as ws
from iff_scheduler.db import supabase_enabled
from iff_scheduler.domain.availability import summarise_availability
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.domain.models import Assignment
from iff_scheduler.review.edit_validator import validate_edits
from iff_scheduler.review.locks import lock_from_assignment, merge_locks
from iff_scheduler.scheduling.base import resolve_panels, resolve_rooms
from iff_scheduler.settings import Settings

router = APIRouter(prefix="/api/workspaces/{workspace_id}/runs/{run_id}", tags=["schedule"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _assignment_id(a: Assignment) -> str:
    return f"{a.applicant_id}:{a.choice_index}"


def _serialise(a: Assignment) -> dict[str, Any]:
    return {"assignment_id": _assignment_id(a), **a.model_dump(mode="json")}


def _declared_availability(workspace_id: str, settings: Settings) -> dict[str, str]:
    """applicant_id -> the day/time preference they ticked on the form, for the
    Applicants view's declared-availability column (FR-51). Best-effort: when
    the clean applicant list is not on this instance the column is left blank
    rather than guessed (CLAUDE.md invariant 3)."""
    path = ws.applicants_clean_path(workspace_id)
    if not path.exists():
        return {}
    slots = build_slot_grid(settings.event).slots
    return {
        a.applicant_id: summarise_availability(a.availability_slots, slots)
        for a in load_clean_applicants(path)
    }


@router.get("/assignments")
def get_assignments(workspace_id: str, run_id: str, settings: SettingsDep) -> list[dict[str, Any]]:
    declared = _declared_availability(workspace_id, settings)

    def serialise(assignments: list[Assignment]) -> list[dict[str, Any]]:
        return [
            {**_serialise(a), "declared_availability": declared.get(a.applicant_id, "")}
            for a in assignments
        ]

    if supabase_enabled():
        from iff_scheduler.db import assignment_repo

        run_pk = resolve_run_pk(workspace_id, run_id)
        return serialise(assignment_repo.list_assignments(run_pk))
    run_dir = resolve_run_dir(workspace_id, run_id)
    path = run_dir / "assignments.csv"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{path} not found — solve first.")
    return serialise(load_assignments(path))


class AssignmentEdit(BaseModel):
    panel_id: str
    slot_id: str


@router.patch("/assignments/{assignment_id}")
def patch_assignment(
    workspace_id: str,
    run_id: str,
    assignment_id: str,
    body: AssignmentEdit,
    settings: SettingsDep,
) -> dict[str, Any]:
    db_mode = supabase_enabled()
    # The run directory is an artefact, not the record. In Supabase mode a run
    # persisted before a redeploy has no directory on this instance, and
    # insisting on one made every edit to it a 404.
    run_dir = (
        run_dir_if_present(workspace_id, run_id)
        if db_mode
        else resolve_run_dir(workspace_id, run_id)
    )
    if db_mode:
        ensure_run_exists(workspace_id, run_id)
    path = (run_dir / "assignments.csv") if run_dir is not None else None
    if not db_mode and (path is None or not path.exists()):
        raise HTTPException(status_code=404, detail=f"{path} not found — solve first.")

    grid = build_slot_grid(settings.event)
    panels = resolve_panels(settings.panels, settings.rooms, grid)
    rooms = resolve_rooms(settings.rooms, grid)
    panels_by_id = {p.id: p for p in panels}
    slots_by_id = {s.slot_id: s for s in grid.slots}

    if body.panel_id not in panels_by_id:
        raise HTTPException(status_code=422, detail=f"Unknown panel '{body.panel_id}'.")
    if body.slot_id not in slots_by_id:
        raise HTTPException(status_code=422, detail=f"Slot '{body.slot_id}' is not on the grid.")

    if db_mode:
        from iff_scheduler.db import assignment_repo

        run_pk = resolve_run_pk(workspace_id, run_id)
        assignments = assignment_repo.list_assignments(run_pk)
    else:
        assert path is not None
        assignments = load_assignments(path)
    target = next((a for a in assignments if _assignment_id(a) == assignment_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"No assignment '{assignment_id}' in this run.")

    panel = panels_by_id[body.panel_id]
    slot = slots_by_id[body.slot_id]

    applicants = {
        a.applicant_id: a
        for a in (
            load_clean_applicants(ws.applicants_clean_path(workspace_id))
            if ws.applicants_clean_path(workspace_id).exists()
            else []
        )
    }
    applicant = applicants.get(target.applicant_id)
    is_clash = (
        body.slot_id not in set(applicant.availability_slots)
        if applicant is not None
        else target.is_clash
    )

    edited = target.model_copy(
        update={
            "panel_id": panel.id,
            "room": panel.room,
            "slot_id": slot.slot_id,
            "date": slot.date,
            "start_time": slot.start_time,
            "end_time": slot.end_time,
            "is_clash": is_clash,
            "is_locked": True,
            "reason": "manual edit (locked, FR-41)",
        }
    )
    edited_list = [edited if a is target else a for a in assignments]

    violations = validate_edits(edited_list, panels, rooms, grid.slots)
    if violations:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"{len(violations)} illegal edit(s) — nothing saved (FR-42, E-12).",
                "violations": [
                    {"applicant_id": v.applicant_id, "code": v.code, "message": v.message}
                    for v in violations
                ],
            },
        )

    if path is not None and path.exists():
        assignments_frame(edited_list).to_csv(path, index=False)
    if db_mode:
        from iff_scheduler.db import assignment_repo

        assignment_repo.update_assignment(
            run_pk,
            edited.applicant_id,
            int(edited.choice_index),
            {
                "panel_id": edited.panel_id,
                "room": edited.room,
                "slot_id": edited.slot_id,
                "date": edited.date.isoformat(),
                "start_time": edited.start_time.isoformat(),
                "end_time": edited.end_time.isoformat(),
                "is_clash": edited.is_clash,
                "is_locked": True,
            },
        )

    # Locks stay in a CSV in both backends: the schema has no locks table and
    # `_build_problem` reads pins from `ws.locks_path` on every re-solve (C6).
    locks_path = ws.locks_path(workspace_id)
    existing = load_locks(locks_path) if locks_path.exists() else []
    merged = merge_locks(existing, [lock_from_assignment(edited)])
    write_locks(merged, locks_path)

    return {
        "assignment": _serialise(edited),
        "locked": True,
        "total_locks": len(merged),
    }


class ResolveBody(BaseModel):
    skip_check: bool = False


@router.post("/resolve")
def resolve(
    workspace_id: str,
    run_id: str,
    settings: SettingsDep,
    body: ResolveBody | None = None,
) -> dict[str, Any]:
    """Re-solve honouring every lock (C6). Writes a fresh run directory."""
    ensure_run_exists(workspace_id, run_id)
    body = body or ResolveBody()
    return execute_solve(settings, workspace_id, skip_check=body.skip_check)
