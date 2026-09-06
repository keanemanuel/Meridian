"""Pipeline orchestration shared by the pipeline and schedule routers.

Every function here is a thin translation of a CLI command body into a
return value + `HTTPException`, calling the exact same core functions
`iff_scheduler.cli` calls (CLAUDE.md: "FastAPI is a thin wrapper").
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import HTTPException

from api.dependencies import config_dir
from iff_scheduler import workspace as ws
from iff_scheduler.cli import (
    _assignments_frame,
    _build_problem,
    _conflicts_frame,
    _load_assignments,
    _load_clean_applicants,
    _load_locks,
    _point_latest_at,
    _previous_run,
    _snapshot_config,
)
from iff_scheduler.db import supabase_enabled
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.scheduling.base import USABLE_STATUSES, Lock, SolveResult
from iff_scheduler.scheduling.feasibility import compute_capacity_advisor, is_feasible
from iff_scheduler.scheduling.postprocess import build_conflicts, compute_metrics, diff_schedules
from iff_scheduler.scheduling.solver_cpsat import CpSatSolver
from iff_scheduler.settings import Settings


def run_capacity_check(settings: Settings, workspace_id: str) -> dict[str, Any]:
    """Capacity Advisor table + verdict (mirrors `iffsched check`, SPEC.md §5.5).

    Unlike the CLI this never hard-exits; the caller decides what INFEASIBLE means.
    """
    applicants_path = ws.applicants_clean_path(workspace_id)
    if not applicants_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No applicants at {applicants_path}. Run ingest first.",
        )
    grid = build_slot_grid(settings.event)
    applicants = _load_clean_applicants(applicants_path)
    rows = compute_capacity_advisor(
        applicants=applicants,
        panels=settings.panels,
        grid=grid,
        target_utilisation=settings.solver.target_utilisation,
    )
    serialised = [
        {
            "division": row.division.value,
            "demand": row.demand,
            "panels_configured": row.panels_configured,
            "raw_supply": row.raw_supply,
            "effective_supply": row.effective_supply,
            "recommended_panels": row.recommended_panels,
            "verdict": row.verdict,
        }
        for row in rows
    ]
    return {
        "feasible": is_feasible(rows),
        "infeasible_divisions": [r.division.value for r in rows if r.verdict == "INFEASIBLE"],
        "rows": serialised,
    }


def execute_solve(settings: Settings, workspace_id: str, *, skip_check: bool) -> dict[str, Any]:
    """Solve the timetable and write an immutable run dir (mirrors `iffsched solve`)."""
    applicants_path = ws.applicants_clean_path(workspace_id)
    if not applicants_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No applicants at {applicants_path}. Run ingest first.",
        )

    grid = build_slot_grid(settings.event)
    applicants = _load_clean_applicants(applicants_path)

    if not skip_check:
        rows = compute_capacity_advisor(
            applicants=applicants,
            panels=settings.panels,
            grid=grid,
            target_utilisation=settings.solver.target_utilisation,
        )
        if not is_feasible(rows):
            infeasible = [r.division.value for r in rows if r.verdict == "INFEASIBLE"]
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Capacity Advisor says INFEASIBLE for {', '.join(infeasible)}. "
                    "Add panels or pass skip_check=true (E-06)."
                ),
            )

    locks_path = ws.locks_path(workspace_id)
    locks: list[Lock] = _load_locks(locks_path) if locks_path.exists() else []

    problem = _build_problem(settings, applicants, locks)
    result: SolveResult = CpSatSolver().solve(problem)

    if result.status not in USABLE_STATUSES:
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    f"No schedule produced: {result.status}. Nothing was written "
                    "(E-18 — a configuration problem, not a data problem)."
                ),
                "log": result.log,
            },
        )

    run_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    runs_dir = ws.runs_dir(workspace_id)
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    previous_dir = _previous_run(runs_dir, run_dir)

    conflicts = build_conflicts(result.assignments, problem.applicants, problem.panels)
    metrics = compute_metrics(result, problem) | {"run_id": run_id}

    _assignments_frame(result.assignments).to_csv(run_dir / "assignments.csv", index=False)
    _conflicts_frame(conflicts).to_csv(run_dir / "conflicts.csv", index=False)
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    (run_dir / "solve.log").write_text("\n".join(result.log) + "\n", encoding="utf-8")
    _snapshot_config(config_dir(), run_dir)

    diff_count = 0
    if previous_dir is not None and (previous_dir / "assignments.csv").exists():
        changes = diff_schedules(
            _load_assignments(previous_dir / "assignments.csv"), result.assignments
        )
        pd.DataFrame([c.__dict__ for c in changes]).to_csv(run_dir / "diff.csv", index=False)
        diff_count = len(changes)

    _point_latest_at(runs_dir, run_dir)

    if supabase_enabled():
        _persist_run_to_db(workspace_id, run_id, metrics, result.assignments)

    return {
        "run_id": run_id,
        "status": result.status,
        "phase": result.phase,
        "interviews_placed": len(result.assignments),
        "interviews_required": 2 * len(applicants),
        "clashes": result.clash_count,
        "locked": int(metrics["locked"]),
        "objective_value": result.objective_value,
        "solve_seconds": round(result.solve_seconds, 3),
        "changed_vs_previous": diff_count,
        "conflicts": len(conflicts),
    }


def _persist_run_to_db(
    workspace_id: str, run_label: str, metrics: dict[str, Any], assignments: list[Any]
) -> None:
    """Mirror a finished solve into Postgres (SPEC.md §14). The run directory
    on disk stays the reproducible artefact (config snapshot, solve log); the
    DB is the queryable system of record the beta web UI reads."""
    from iff_scheduler.db import assignment_repo, run_repo, workspace_repo

    pk = workspace_repo.get_workspace_id(workspace_id)
    if pk is None:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found.")
    run_row = run_repo.create_run(pk, run_label, status="complete", metrics=metrics)
    assignment_repo.replace_assignments(run_row["id"], assignments)


def read_run_metrics(run_dir: Path) -> dict[str, Any]:
    metrics_path = run_dir / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8")) if metrics_path.exists() else {}
    files = sorted(p.name for p in run_dir.iterdir() if p.is_file())
    return {
        "run_id": run_dir.resolve().name,
        "files": files,
        "metrics": metrics,
    }
