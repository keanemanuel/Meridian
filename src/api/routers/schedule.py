"""Assignment viewing, manual single-assignment edits, and re-solve
(FR-40..FR-42, SPEC.md §12).

A manual edit here does exactly what `iffsched lock` + `iffsched solve` do:
the whole edited schedule is run through `validate_edits` (E-12) and, only
if it is legal, the touched choice is written to
`locks/pinned_assignments.csv` so every subsequent solve honours it (C6).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
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
from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import SlotGrid, build_slot_grid
from iff_scheduler.domain.models import Assignment, Panel
from iff_scheduler.review.edit_validator import validate_edits
from iff_scheduler.review.locks import lock_from_assignment, merge_locks
from iff_scheduler.scheduling.base import resolve_panels, resolve_rooms
from iff_scheduler.settings import PanelsConfig, Settings

router = APIRouter(prefix="/api/workspaces/{workspace_id}/runs/{run_id}", tags=["schedule"])

SettingsDep = Annotated[Settings, Depends(get_settings)]


def _assignment_id(a: Assignment) -> str:
    return f"{a.applicant_id}:{a.choice_index}"


def _serialise(a: Assignment) -> dict[str, Any]:
    return {"assignment_id": _assignment_id(a), **a.model_dump(mode="json")}


# --------------------------------------------------------- manual panels
#
# A recruiter can add an empty panel to a room from the Rooms tab (a division
# with no interviews yet, ready for drag-and-drop). It has no assignments, so
# nothing in the run's own data records it — it lives in this small artefact
# beside `assignments.csv`, one entry per manually-added panel:
#   [{"id": "PROGRAM-A2", "division": "PROGRAM", "room": "2016"}]
# `_run_panels` folds it into the panel set every move is validated against, so
# an interview can be dragged onto it; a re-solve starts from committed config
# again and does not carry empty manual panels forward (by design — the
# automated solve never produces a room this shape).


def _manual_panels_path(run_dir: Path) -> Path:
    return run_dir / "manual_panels.json"


def _load_manual_panels(run_dir: Path | None) -> list[dict[str, str]]:
    if run_dir is None:
        return []
    path = _manual_panels_path(run_dir)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data) if isinstance(data, list) else []


def _write_manual_panels(run_dir: Path, entries: list[dict[str, str]]) -> None:
    _manual_panels_path(run_dir).write_text(
        json.dumps(entries, indent=2), encoding="utf-8"
    )


# A recruiter can also drag a whole panel from one room card to another on the
# Rooms tab — only its room changes, every interview stays on it. A solver
# panel's room lives in `metrics["solved_panels"]` / config, so the override is
# recorded here, `{panel_id: new_room}`, and `_run_panels` folds it back in. A
# manually-added panel carries its own room in `manual_panels.json`, so its move
# is written there instead. Neither artefact survives a re-solve — the automated
# solve starts from committed config again (Session G1).


def _panel_moves_path(run_dir: Path) -> Path:
    return run_dir / "panel_room_moves.json"


def _load_panel_moves(run_dir: Path | None) -> dict[str, str]:
    if run_dir is None:
        return {}
    path = _panel_moves_path(run_dir)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data) if isinstance(data, dict) else {}


def _write_panel_moves(run_dir: Path, moves: dict[str, str]) -> None:
    _panel_moves_path(run_dir).write_text(
        json.dumps(moves, indent=2), encoding="utf-8"
    )


def _room_day_letter(settings: Settings, grid: SlotGrid, room_id: str) -> str:
    """"A" for the first event day, "B" for the second — matching the canonical
    `[DIVISION]-[DAY][N]` panel-id format (settings.PanelEntry). A room open on
    more than one day takes the letter of its earliest."""
    ordered = sorted({slot.date for slot in grid.slots})
    room = next((r for r in settings.rooms.rooms if r.id == room_id), None)
    day = min(room.days) if room and room.days else (ordered[0] if ordered else None)
    idx = ordered.index(day) if day in ordered else 0
    return chr(ord("A") + idx)


def _next_manual_panel_id(
    division: str, letter: str, existing_ids: set[str]
) -> str:
    """Next free `[DIVISION]-[DAY][N]` for this division on that day."""
    n = 1
    while f"{division}-{letter}{n}" in existing_ids:
        n += 1
    return f"{division}-{letter}{n}"


def _run_panels(
    settings: Settings,
    grid: SlotGrid,
    *,
    assignments: list[Assignment],
    run_dir: Path | None,
    run_metrics: dict[str, Any] | None,
    manual_panels: list[dict[str, str]] | None = None,
    panel_moves: dict[str, str] | None = None,
) -> list[Panel]:
    """The panel set a manual edit to this run must be validated against.

    Every solve runs `rebalance_panels`/`autoscale_panels`, which append extra
    panels to the committed config before the problem reaches CP-SAT — ids
    like ``MEDMARDOC-A2`` or ``PROGRAM-B3`` (next free number for the division
    that day, ``origin="balanced"``) that never appear in ``panels.yaml``. The
    run's own assignments carry those ids, and so do the
    panels the move UIs offer (frontend ``divisionPanels``, derived from the
    same assignments). Validating against the bare committed config therefore
    rejects every move onto a load-balanced panel as "Unknown panel", and a
    re-solve never fixes it because the next solve regenerates the same extra
    panels and still never writes them to config.

    Two layers, so the answer is right no matter what metadata survived:

    1. The recorded solved set — ``metrics["solved_panels"]`` (this session
       onward), else the legacy ``autoscale.json``, else committed config.
       This carries the exact active-slot windows the solver used, so C4/C7
       edit checks stay accurate for panels it covers.
    2. A backfill from the run's *own assignments* for any panel id they place
       an interview on that layer 1 missed — a pre-fix run, a run whose
       directory a redeploy wiped and whose DB row predates ``solved_panels``,
       or any other stale/absent record. These are the CURRENT live panels by
       definition; nothing the run actually scheduled can be "unknown". Their
       active slots are every grid slot on the day(s) the panel runs, within
       its room's open days — matching how a load-balanced (`origin="balanced"`)
       panel is resolved from its full-evening active window.
    """
    entries: list[dict[str, Any]] | None = None

    metrics = run_metrics
    if metrics is None and run_dir is not None:
        metrics_path = run_dir / "metrics.json"
        if metrics_path.exists():
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    if metrics and metrics.get("solved_panels"):
        entries = metrics["solved_panels"]
    elif run_dir is not None and (run_dir / "autoscale.json").exists():
        legacy = json.loads((run_dir / "autoscale.json").read_text(encoding="utf-8"))
        entries = legacy.get("panels") or None

    panels_config = PanelsConfig.model_validate({"panels": entries}) if entries else settings.panels
    panels = resolve_panels(panels_config, settings.rooms, grid)

    known = {p.id for p in panels}
    all_dates = {slot.date for slot in grid.slots}
    room_days = {r.id: (set(r.days) if r.days else set(all_dates)) for r in settings.rooms.rooms}

    # Manually-added empty panels (Rooms tab). Active for every grid slot on the
    # day(s) its room is open — same resolution a load-balanced panel gets.
    for entry in manual_panels or []:
        if entry["id"] in known:
            continue
        allowed = room_days.get(entry["room"], set(all_dates))
        panels.append(
            Panel(
                id=entry["id"],
                division=DivisionCode(entry["division"]),
                room=entry["room"],
                active_slot_ids=[s.slot_id for s in grid.slots if s.date in allowed],
            )
        )
        known.add(entry["id"])

    missing: dict[str, dict[str, Any]] = {}
    for a in assignments:
        if a.panel_id in known:
            continue
        meta = missing.setdefault(
            a.panel_id, {"division": a.division, "room": a.room, "dates": set()}
        )
        meta["dates"].add(a.date)
    for panel_id, meta in missing.items():
        allowed = room_days.get(meta["room"], set(all_dates)) & meta["dates"]
        active = [slot.slot_id for slot in grid.slots if slot.date in allowed]
        panels.append(
            Panel(
                id=panel_id,
                division=meta["division"],
                room=meta["room"],
                active_slot_ids=active,
            )
        )

    # Manual panel-to-room moves (Rooms tab drag-and-drop). Only the room
    # changes; the active-slot window is kept — the move endpoint only allows a
    # target room open on every day the panel runs, so the same slots stay
    # valid. A move whose entry has been overtaken (e.g. the manual panel's own
    # room already updated) is a harmless no-op.
    moves = panel_moves or {}
    if moves:
        slot_date = {s.slot_id: s.date for s in grid.slots}
        remapped: list[Panel] = []
        for p in panels:
            dest = moves.get(p.id)
            if not dest or dest == p.room:
                remapped.append(p)
                continue
            allowed = room_days.get(dest, all_dates)
            active = [s for s in p.active_slot_ids if slot_date.get(s) in allowed]
            remapped.append(
                p.model_copy(
                    update={"room": dest, "active_slot_ids": active or p.active_slot_ids}
                )
            )
        panels = remapped
    return panels


def _applicant_availability(
    workspace_id: str, settings: Settings
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """(preference summary, raw available slot-ids) per applicant.

    The summary feeds the Applicants view's declared-availability column
    (FR-51); the raw slot-id list lets the move dialog and drag-and-drop flag a
    target that sits outside the applicant's stated availability as a clash
    without blocking it (FR-40, FR-34). Best-effort: when the clean applicant
    list is not on this instance both are left empty rather than guessed
    (CLAUDE.md invariant 3)."""
    path = ws.applicants_clean_path(workspace_id)
    if not path.exists():
        return {}, {}
    slots = build_slot_grid(settings.event).slots
    applicants = load_clean_applicants(path)
    declared = {
        a.applicant_id: summarise_availability(a.availability_slots, slots) for a in applicants
    }
    available = {a.applicant_id: list(a.availability_slots) for a in applicants}
    return declared, available


@router.get("/assignments")
def get_assignments(workspace_id: str, run_id: str, settings: SettingsDep) -> list[dict[str, Any]]:
    declared, available = _applicant_availability(workspace_id, settings)

    def serialise(assignments: list[Assignment]) -> list[dict[str, Any]]:
        return [
            {
                **_serialise(a),
                "declared_availability": declared.get(a.applicant_id, ""),
                "availability_slots": available.get(a.applicant_id, []),
            }
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

    # Load this run's assignments first: they are the authoritative, live panel
    # set (every panel_id/room the solver actually used is on them), and the
    # move UIs only ever offer a panel that appears here.
    run_pk: str | None = None
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

    # Validate against the panels *this run* was solved with — committed config
    # plus any load-balanced (`origin="balanced"`) panels — never the bare
    # committed config, or every move onto a load-balanced panel is a false
    # "Unknown panel" that no re-solve can clear (FR-40..FR-42).
    run_metrics: dict[str, Any] | None = None
    if db_mode and run_dir is None:
        from api.dependencies import workspace_pk
        from iff_scheduler.db import run_repo

        row = run_repo.get_run(workspace_pk(workspace_id), run_id)
        run_metrics = (row or {}).get("metrics") or None
    panels = _run_panels(
        settings,
        grid,
        assignments=assignments,
        run_dir=run_dir,
        run_metrics=run_metrics,
        manual_panels=_load_manual_panels(run_dir),
        panel_moves=_load_panel_moves(run_dir),
    )
    rooms = resolve_rooms(settings.rooms, grid)
    panels_by_id = {p.id: p for p in panels}
    slots_by_id = {s.slot_id: s for s in grid.slots}

    if body.panel_id not in panels_by_id:
        raise HTTPException(status_code=422, detail=f"Unknown panel '{body.panel_id}'.")
    if body.slot_id not in slots_by_id:
        raise HTTPException(status_code=422, detail=f"Slot '{body.slot_id}' is not on the grid.")

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

    # A manual move acts on an explicit recruiter instruction, so the C4
    # "4-panel cap" is not enforced here — the recruiter may deliberately
    # overload a room (e.g. drag an interview onto a panel they just added as a
    # 5th in that room). Every other edit check still applies.
    violations = validate_edits(
        edited_list, panels, rooms, grid.slots, enforce_room_capacity=False
    )
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

        assert run_pk is not None
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


class LockEdit(BaseModel):
    locked: bool


@router.patch("/assignments/{assignment_id}/lock")
def set_assignment_lock(
    workspace_id: str,
    run_id: str,
    assignment_id: str,
    body: LockEdit,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Lock or unlock a single interview by hand (FR-41).

    Only the lock flag changes — panel, room and slot stay exactly where they
    are — so this skips the geometry checks `patch_assignment` runs. Locking
    pins the choice in `locks/pinned_assignments.csv` so every later re-solve
    keeps it (C6); unlocking drops that pin so the solver may move it again.
    Idempotent: re-sending the state a choice is already in just rewrites the
    same lock set.
    """
    db_mode = supabase_enabled()
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

    run_pk: str | None = None
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

    edited = target.model_copy(
        update={
            "is_locked": body.locked,
            "reason": "manual lock (FR-41)" if body.locked else "manual unlock (FR-41)",
        }
    )
    edited_list = [edited if a is target else a for a in assignments]

    if path is not None and path.exists():
        assignments_frame(edited_list).to_csv(path, index=False)
    if db_mode:
        from iff_scheduler.db import assignment_repo

        assert run_pk is not None
        assignment_repo.update_assignment(
            run_pk,
            edited.applicant_id,
            int(edited.choice_index),
            {"is_locked": body.locked},
        )

    # Locks live in a CSV in both backends — `_build_problem` reads pins from
    # `ws.locks_path` on every re-solve (C6).
    locks_path = ws.locks_path(workspace_id)
    existing = load_locks(locks_path) if locks_path.exists() else []
    if body.locked:
        merged = merge_locks(existing, [lock_from_assignment(edited)])
    else:
        merged = [
            lock
            for lock in existing
            if (lock.applicant_id, lock.choice_index)
            != (edited.applicant_id, edited.choice_index)
        ]
    write_locks(merged, locks_path)

    return {
        "assignment": _serialise(edited),
        "locked": body.locked,
        "total_locks": len(merged),
    }


# --------------------------------------------------------- panel management
#
# Per-room manual panel control for the Rooms tab (FR-40..FR-42). Add an empty
# panel for a division that has none in a room; delete one only while its
# schedule is still empty. The 4-panel cap (C4) is not enforced on an add — a
# recruiter may deliberately run a 5th panel in a room.


def _load_run_assignments(
    workspace_id: str, run_id: str
) -> tuple[list[Assignment], Path | None]:
    """This run's assignments plus its directory (``None`` in DB mode when the
    run predates this instance) — the shared preamble of every panel route."""
    db_mode = supabase_enabled()
    run_dir = (
        run_dir_if_present(workspace_id, run_id)
        if db_mode
        else resolve_run_dir(workspace_id, run_id)
    )
    if db_mode:
        ensure_run_exists(workspace_id, run_id)
        from iff_scheduler.db import assignment_repo

        return assignment_repo.list_assignments(resolve_run_pk(workspace_id, run_id)), run_dir
    path = run_dir / "assignments.csv" if run_dir is not None else None
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail=f"{path} not found — solve first.")
    return load_assignments(path), run_dir


def _panel_rows(settings: Settings, workspace_id: str, run_id: str) -> list[dict[str, Any]]:
    """Every panel this run carries — solver panels plus manually-added empty
    ones — with its interview count and whether the recruiter may delete it
    (manual and still empty)."""
    assignments, run_dir = _load_run_assignments(workspace_id, run_id)
    grid = build_slot_grid(settings.event)
    manual = _load_manual_panels(run_dir)
    manual_ids = {e["id"] for e in manual}
    panels = _run_panels(
        settings,
        grid,
        assignments=assignments,
        run_dir=run_dir,
        run_metrics=None,
        manual_panels=manual,
        panel_moves=_load_panel_moves(run_dir),
    )
    counts = Counter(a.panel_id for a in assignments)
    rows = [
        {
            "panel_id": p.id,
            "division": p.division.value,
            "room": p.room,
            "interview_count": counts.get(p.id, 0),
            "manual": p.id in manual_ids,
            "deletable": p.id in manual_ids and counts.get(p.id, 0) == 0,
        }
        for p in panels
    ]
    rows.sort(key=lambda r: (str(r["room"]), str(r["panel_id"])))
    return rows


@router.get("/panels")
def list_panels(workspace_id: str, run_id: str, settings: SettingsDep) -> dict[str, Any]:
    return {
        "panels": _panel_rows(settings, workspace_id, run_id),
        "divisions": [d.code.value for d in settings.divisions.divisions],
    }


class PanelCreate(BaseModel):
    division: str
    room: str


@router.post("/panels")
def create_panel(
    workspace_id: str, run_id: str, body: PanelCreate, settings: SettingsDep
) -> dict[str, Any]:
    assignments, run_dir = _load_run_assignments(workspace_id, run_id)
    if run_dir is None:
        raise HTTPException(
            status_code=409,
            detail="This run has no directory on this instance, so a panel cannot be added to it.",
        )

    try:
        division = DivisionCode(body.division)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"Unknown division '{body.division}'."
        ) from None
    room = next((r for r in settings.rooms.rooms if r.id == body.room), None)
    if room is None:
        raise HTTPException(status_code=422, detail=f"Unknown room '{body.room}'.")
    if division not in room.divisions:
        raise HTTPException(
            status_code=422,
            detail=f"Room '{room.id}' is not configured for {division.value}.",
        )

    rows = _panel_rows(settings, workspace_id, run_id)
    if any(r["division"] == division.value and r["room"] == room.id for r in rows):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{division.value} already has a panel in room {room.id} — a room cannot "
                "run two panels of the same division (room-exclusivity)."
            ),
        )

    grid = build_slot_grid(settings.event)
    letter = _room_day_letter(settings, grid, room.id)
    existing_ids = {r["panel_id"] for r in rows}
    new_id = _next_manual_panel_id(division.value, letter, existing_ids)

    manual = _load_manual_panels(run_dir)
    manual.append({"id": new_id, "division": division.value, "room": room.id})
    _write_manual_panels(run_dir, manual)

    return {
        "panel": {"panel_id": new_id, "division": division.value, "room": room.id},
        "panels": _panel_rows(settings, workspace_id, run_id),
    }


@router.delete("/panels/{panel_id}")
def delete_panel(
    workspace_id: str, run_id: str, panel_id: str, settings: SettingsDep
) -> dict[str, Any]:
    assignments, run_dir = _load_run_assignments(workspace_id, run_id)
    if run_dir is None:
        raise HTTPException(
            status_code=409,
            detail="This run has no directory on this instance, so its panels cannot be edited.",
        )
    manual = _load_manual_panels(run_dir)
    if not any(e["id"] == panel_id for e in manual):
        raise HTTPException(
            status_code=404,
            detail=(
                f"'{panel_id}' is not a manually-added panel. A solver panel disappears on "
                "its own once every interview is moved off it."
            ),
        )
    booked = sum(1 for a in assignments if a.panel_id == panel_id)
    if booked:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Panel '{panel_id}' still has {booked} interview(s). Move them out before "
                "deleting it."
            ),
        )
    _write_manual_panels(run_dir, [e for e in manual if e["id"] != panel_id])
    return {"deleted": panel_id, "panels": _panel_rows(settings, workspace_id, run_id)}


class PanelMove(BaseModel):
    room: str


@router.patch("/panels/{panel_id}")
def move_panel(
    workspace_id: str,
    run_id: str,
    panel_id: str,
    body: PanelMove,
    settings: SettingsDep,
) -> dict[str, Any]:
    """Relocate a whole panel to another room from the Rooms tab (FR-40..FR-42).

    Only the room changes — every interview already on the panel stays on it, at
    the same panel id and slot. Room-exclusivity is still enforced (409 if the
    target room already runs this division); the C4 4-panel cap is NOT,
    consistent with every other manual Rooms-tab action (Session G4). A target
    room must be open on every day the panel runs.
    """
    db_mode = supabase_enabled()
    assignments, run_dir = _load_run_assignments(workspace_id, run_id)
    if run_dir is None:
        raise HTTPException(
            status_code=409,
            detail="This run has no directory on this instance, so its panels cannot be moved.",
        )

    grid = build_slot_grid(settings.event)
    manual = _load_manual_panels(run_dir)
    moves = _load_panel_moves(run_dir)
    panels = _run_panels(
        settings,
        grid,
        assignments=assignments,
        run_dir=run_dir,
        run_metrics=None,
        manual_panels=manual,
        panel_moves=moves,
    )
    panel = next((p for p in panels if p.id == panel_id), None)
    if panel is None:
        raise HTTPException(status_code=404, detail=f"No panel '{panel_id}' in this run.")

    dest = next((r for r in settings.rooms.rooms if r.id == body.room), None)
    if dest is None:
        raise HTTPException(status_code=422, detail=f"Unknown room '{body.room}'.")
    if dest.id == panel.room:
        raise HTTPException(
            status_code=400, detail=f"Panel '{panel_id}' is already in room {dest.id}."
        )
    if panel.division not in dest.divisions:
        raise HTTPException(
            status_code=422,
            detail=f"Room '{dest.id}' is not configured for {panel.division.value}.",
        )

    all_dates = {s.date for s in grid.slots}
    slot_date = {s.slot_id: s.date for s in grid.slots}
    dest_days = set(dest.days) if dest.days else set(all_dates)
    panel_dates = {slot_date[s] for s in panel.active_slot_ids if s in slot_date} | {
        a.date for a in assignments if a.panel_id == panel_id
    }
    off_day = sorted(str(d) for d in panel_dates - dest_days)
    if off_day:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Room '{dest.id}' is not open on {', '.join(off_day)} — panel "
                f"'{panel_id}' runs then."
            ),
        )

    if any(
        p.id != panel_id and p.division == panel.division and p.room == dest.id
        for p in panels
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"{panel.division.value} already has a panel in room {dest.id} — a room "
                "cannot run two panels of the same division (room-exclusivity)."
            ),
        )

    moved_panels = [
        p.model_copy(update={"room": dest.id}) if p.id == panel_id else p for p in panels
    ]
    edited_list = [
        a.model_copy(update={"room": dest.id}) if a.panel_id == panel_id else a
        for a in assignments
    ]
    rooms = resolve_rooms(settings.rooms, grid)
    # An explicit recruiter instruction, so the C4 room cap is not enforced —
    # the panel may land in an already-full room. Every other edit check still
    # applies (division mismatch, off-grid slot, unknown room, ...).
    violations = validate_edits(
        edited_list, moved_panels, rooms, grid.slots, enforce_room_capacity=False
    )
    if violations:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"{len(violations)} illegal edit(s) — nothing moved (FR-42, E-12).",
                "violations": [
                    {"applicant_id": v.applicant_id, "code": v.code, "message": v.message}
                    for v in violations
                ],
            },
        )

    # Persist the room change: a manually-added panel carries its room in
    # manual_panels.json; a solver panel's override goes to panel_room_moves.json.
    if any(e["id"] == panel_id for e in manual):
        for e in manual:
            if e["id"] == panel_id:
                e["room"] = dest.id
        _write_manual_panels(run_dir, manual)
    else:
        moves[panel_id] = dest.id
        _write_panel_moves(run_dir, moves)

    moved = sum(1 for a in assignments if a.panel_id == panel_id)
    csv_path = run_dir / "assignments.csv"
    if csv_path.exists():
        assignments_frame(edited_list).to_csv(csv_path, index=False)
    if db_mode:
        from iff_scheduler.db import assignment_repo

        run_pk = resolve_run_pk(workspace_id, run_id)
        for a in assignments:
            if a.panel_id == panel_id:
                assignment_repo.update_assignment(
                    run_pk, a.applicant_id, int(a.choice_index), {"room": dest.id}
                )

    rows = _panel_rows(settings, workspace_id, run_id)
    return {
        "panel": next((r for r in rows if r["panel_id"] == panel_id), None),
        "panels": rows,
        "moved_interviews": moved,
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
