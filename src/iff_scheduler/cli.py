"""Command-line interface (SPEC.md §7.1)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import cast

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from iff_scheduler.domain.enums import DivisionCode, Severity
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.domain.models import Applicant, Assignment, Conflict
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
from iff_scheduler.ingest.validate import run_ingest, write_outputs
from iff_scheduler.review.edit_validator import validate_edits
from iff_scheduler.review.locks import (
    LOCK_COLUMNS,
    lock_from_assignment,
    lock_to_row,
    merge_locks,
    parse_lock_rows,
)
from iff_scheduler.scheduling.base import (
    USABLE_STATUSES,
    Lock,
    SolveProblem,
    SolveResult,
    resolve_panels,
    resolve_rooms,
)
from iff_scheduler.scheduling.feasibility import compute_capacity_advisor, is_feasible
from iff_scheduler.scheduling.postprocess import (
    build_conflicts,
    compute_metrics,
    diff_schedules,
)
from iff_scheduler.scheduling.solver_cpsat import CpSatSolver
from iff_scheduler.settings import DEFAULT_CONFIG_DIR, Settings, load_settings

app = typer.Typer(add_completion=False, help="IFF recruitment interview scheduler.")
console = Console()


@app.callback()
def _main() -> None:
    """IFF recruitment interview scheduler (SPEC.md §7.1)."""


@app.command()
def ingest(
    source: str = typer.Option("csv", "--source", help="Applicant data source: csv|sheets"),
    input_path: Path = typer.Option(
        None, "--input", help="Raw CSV export (required for --source csv)"
    ),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
    out_dir: Path = typer.Option(Path("data/interim"), "--out-dir"),
) -> None:
    """Ingest applicant data, normalise it, and write a validation report (FR-01..FR-07)."""
    if source != "csv":
        console.print(f"[red]Source '{source}' is not implemented yet. Use --source csv.[/red]")
        raise typer.Exit(code=1)
    if input_path is None:
        console.print("[red]--input is required when --source csv[/red]")
        raise typer.Exit(code=1)
    if not input_path.exists():
        console.print(f"[red]Input file not found: {input_path}[/red]")
        raise typer.Exit(code=1)

    settings = load_settings(config_dir)
    grid = build_slot_grid(settings.event)
    result = run_ingest(
        source=CsvApplicantSource(path=input_path),
        event=settings.event,
        divisions=settings.divisions,
        grid=grid,
    )

    clean_path = out_dir / "applicants.clean.csv"
    report_path = out_dir / "validation_report.csv"
    write_outputs(result, clean_path=clean_path, report_path=report_path)

    rejected = sum(1 for row in result.report if row.outcome == "REJECTED")
    collapsed = sum(1 for row in result.report if row.outcome == "COLLAPSED")
    warnings = sum(1 for row in result.report if row.outcome == "WARNING")

    table = Table(title="Ingest summary")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    table.add_row("Clean applicants", str(len(result.applicants)))
    table.add_row("Rejected", str(rejected))
    table.add_row("Collapsed duplicates", str(collapsed))
    table.add_row("Warnings", str(warnings))
    console.print(table)
    console.print(f"Wrote {clean_path}")
    console.print(f"Wrote {report_path}")


def _load_clean_applicants(path: Path) -> list[Applicant]:
    """Read applicants.clean.csv back into Applicant objects (the inverse of
    ingest.validate.write_outputs)."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
    applicants = []
    for row in df.to_dict(orient="records"):
        applicants.append(
            Applicant(
                applicant_id=row["applicant_id"],
                full_name=row["full_name"],
                email=row["email"],
                phone=row["phone"],
                sub_division_1=row["sub_division_1"],
                sub_division_2=row["sub_division_2"],
                division_1=DivisionCode(row["division_1"]),
                division_2=DivisionCode(row["division_2"]),
                availability_slots=[s for s in row["availability_slots"].split("|") if s],
                submitted_at=datetime.fromisoformat(row["submitted_at"]),
                notes=row["notes"] or None,
            )
        )
    return applicants


VERDICT_STYLE = {"OK": "green", "TIGHT": "yellow", "INFEASIBLE": "bold red"}


@app.command()
def check(
    input_path: Path = typer.Option(
        Path("data/interim/applicants.clean.csv"),
        "--input",
        help="applicants.clean.csv produced by `iffsched ingest`",
    ),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
) -> None:
    """Capacity Advisor — demand vs. panel-slot supply per division (SPEC.md §5.5).

    Hard-stops with exit code 1 if any division is INFEASIBLE, so a bad
    panel/room configuration is caught before the solver runs (E-06)."""
    if not input_path.exists():
        console.print(
            f"[red]Input file not found: {input_path}. Run `iffsched ingest` first.[/red]"
        )
        raise typer.Exit(code=1)

    settings = load_settings(config_dir)
    grid = build_slot_grid(settings.event)
    applicants = _load_clean_applicants(input_path)

    rows = compute_capacity_advisor(
        applicants=applicants,
        panels=settings.panels,
        grid=grid,
        target_utilisation=settings.solver.target_utilisation,
    )

    table = Table(title="Capacity Advisor")
    table.add_column("Division")
    table.add_column("Demand", justify="right")
    table.add_column("Panels configured", justify="right")
    table.add_column("Raw supply", justify="right")
    table.add_column("Effective supply", justify="right")
    table.add_column("Recommended panels", justify="right")
    table.add_column("Verdict")
    for row in rows:
        style = VERDICT_STYLE[row.verdict]
        table.add_row(
            row.division.value,
            str(row.demand),
            str(row.panels_configured),
            str(row.raw_supply),
            str(row.effective_supply),
            str(row.recommended_panels),
            f"[{style}]{row.verdict}[/{style}]",
        )
    console.print(table)

    if not is_feasible(rows):
        infeasible = ", ".join(row.division.value for row in rows if row.verdict == "INFEASIBLE")
        console.print(
            f"[bold red]INFEASIBLE: {infeasible}. Spawn more panels, add rooms, or adjust "
            "the event grid before solving (SPEC.md §1.2 Finding A).[/bold red]"
        )
        raise typer.Exit(code=1)


# ------------------------------------------------------------------ solve


ASSIGNMENT_COLUMNS = [
    "applicant_id",
    "full_name",
    "email",
    "choice_index",
    "sub_division",
    "division",
    "panel_id",
    "room",
    "slot_id",
    "date",
    "start_time",
    "end_time",
    "is_clash",
    "is_locked",
    "same_parent_pair",
    "reason",
]


def _load_locks(path: Path) -> list[Lock]:
    """Read pinned assignments (C6, FR-41). Accepts any CSV carrying the four
    columns that identify a pin, so `runs/<ts>/assignments.csv` can be fed
    straight back in via `iffsched lock`."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
    rows = cast("list[dict[str, str]]", df.to_dict(orient="records"))
    try:
        return parse_lock_rows(rows)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _write_locks(locks: list[Lock], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [lock_to_row(lock) for lock in locks]
    pd.DataFrame(rows, columns=LOCK_COLUMNS).to_csv(path, index=False)


def _assignments_frame(assignments: list[Assignment]) -> pd.DataFrame:
    rows = [
        {
            **a.model_dump(mode="json"),
        }
        for a in assignments
    ]
    return pd.DataFrame(rows, columns=ASSIGNMENT_COLUMNS)


CONFLICT_COLUMNS = ["applicant_id", "severity", "type", "message"]


def _conflicts_frame(conflicts: list[Conflict]) -> pd.DataFrame:
    rows = [c.model_dump(mode="json") for c in conflicts]
    return pd.DataFrame(rows, columns=CONFLICT_COLUMNS)


def _snapshot_config(config_dir: Path, run_dir: Path) -> None:
    """Every run keeps the exact config it used, so any published schedule can
    be reproduced later (CLAUDE.md, "Conventions")."""
    snapshot = run_dir / "config_snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    for yaml_file in sorted(config_dir.glob("*.yaml")):
        shutil.copy2(yaml_file, snapshot / yaml_file.name)


def _point_latest_at(runs_dir: Path, run_dir: Path) -> None:
    """`runs/latest` — what `--run latest` resolves to downstream."""
    latest = runs_dir / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(run_dir.name)


def _previous_run(runs_dir: Path, current: Path) -> Path | None:
    candidates = sorted(
        d for d in runs_dir.glob("*") if d.is_dir() and not d.is_symlink() and d != current
    )
    return candidates[-1] if candidates else None


def _build_problem(
    settings: Settings, applicants: list[Applicant], locks: list[Lock]
) -> SolveProblem:
    grid = build_slot_grid(settings.event)
    return SolveProblem(
        applicants=applicants,
        panels=resolve_panels(settings.panels, grid),
        rooms=resolve_rooms(settings.rooms),
        slots=grid.slots,
        weights=settings.solver.weights,
        min_gap_slots=settings.event.min_gap_slots,
        locks=locks,
        two_phase=settings.solver.two_phase,
        time_limit_seconds=float(settings.solver.time_limit_seconds),
        phase1_time_fraction=settings.solver.phase1_time_fraction,
        random_seed=settings.solver.random_seed,
    )


@app.command()
def solve(
    input_path: Path = typer.Option(
        Path("data/interim/applicants.clean.csv"),
        "--input",
        help="applicants.clean.csv produced by `iffsched ingest`",
    ),
    solver: str = typer.Option("cpsat", "--solver", help="Solver to use: cpsat|greedy"),
    locks_path: Path = typer.Option(
        Path("data/locks/pinned_assignments.csv"),
        "--locks",
        help="Pinned assignments the solver must honour (C6). Ignored if absent.",
    ),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir"),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
    skip_check: bool = typer.Option(
        False, "--skip-check", help="Solve even if the Capacity Advisor says INFEASIBLE (E-06)."
    ),
) -> None:
    """Solve the timetable and write an immutable run directory (FR-30..FR-39)."""
    if solver == "greedy":
        console.print(
            "[red]The greedy fallback (SPEC.md §5.3) is not implemented yet. "
            "Use --solver cpsat.[/red]"
        )
        raise typer.Exit(code=1)
    if solver != "cpsat":
        console.print(f"[red]Unknown solver '{solver}'. Use cpsat.[/red]")
        raise typer.Exit(code=1)
    if not input_path.exists():
        console.print(
            f"[red]Input file not found: {input_path}. Run `iffsched ingest` first.[/red]"
        )
        raise typer.Exit(code=1)

    settings = load_settings(config_dir)
    grid = build_slot_grid(settings.event)
    applicants = _load_clean_applicants(input_path)

    if not skip_check:
        rows = compute_capacity_advisor(
            applicants=applicants,
            panels=settings.panels,
            grid=grid,
            target_utilisation=settings.solver.target_utilisation,
        )
        if not is_feasible(rows):
            infeasible = ", ".join(r.division.value for r in rows if r.verdict == "INFEASIBLE")
            console.print(
                f"[bold red]Capacity Advisor says INFEASIBLE for {infeasible}. Run "
                "`iffsched check` for the table, then add panels — or pass --skip-check "
                "to solve anyway (E-06).[/bold red]"
            )
            raise typer.Exit(code=1)

    locks: list[Lock] = []
    if locks_path.exists():
        locks = _load_locks(locks_path)
        console.print(f"Honouring {len(locks)} locked assignment(s) from {locks_path} (C6).")

    problem = _build_problem(settings, applicants, locks)

    run_id = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    console.print(
        f"Solving {len(applicants)} applicants x 2 choices = {2 * len(applicants)} interviews "
        f"across {len(problem.slots)} slots and {len(problem.panels)} panels..."
    )
    result: SolveResult = CpSatSolver().solve(problem)

    if result.status not in USABLE_STATUSES:
        for line in result.log:
            console.print(f"  {line}")
        console.print(
            f"[bold red]No schedule produced: {result.status}. Nothing was written. "
            "This is a configuration problem, not a data problem (E-18) — check panel "
            "counts, active windows and locks.[/bold red]"
        )
        raise typer.Exit(code=1)

    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    previous_dir = _previous_run(runs_dir, run_dir)

    conflicts = build_conflicts(result.assignments, problem.applicants, problem.panels)
    metrics = compute_metrics(result, problem) | {"run_id": run_id}

    _assignments_frame(result.assignments).to_csv(run_dir / "assignments.csv", index=False)
    _conflicts_frame(conflicts).to_csv(run_dir / "conflicts.csv", index=False)
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    (run_dir / "solve.log").write_text("\n".join(result.log) + "\n", encoding="utf-8")
    _snapshot_config(config_dir, run_dir)

    if previous_dir is not None and (previous_dir / "assignments.csv").exists():
        changes = diff_schedules(
            _load_assignments(previous_dir / "assignments.csv"), result.assignments
        )
        pd.DataFrame([c.__dict__ for c in changes]).to_csv(run_dir / "diff.csv", index=False)
        console.print(f"{len(changes)} assignment(s) changed vs {previous_dir.name} (diff.csv).")

    _point_latest_at(runs_dir, run_dir)

    table = Table(title=f"Solve {run_id}")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Status", f"{result.status} (phase {result.phase})")
    table.add_row("Interviews placed", f"{len(result.assignments)} / {2 * len(applicants)}")
    table.add_row("Clashes", str(result.clash_count))
    table.add_row("Locked", str(metrics["locked"]))
    table.add_row("Objective", str(result.objective_value))
    table.add_row("Solve seconds", f"{result.solve_seconds:.2f}")
    console.print(table)

    for line in result.log:
        console.print(f"  {line}")
    if result.clash_count:
        console.print(
            f"[bold red]{result.clash_count} interview(s) fall outside declared availability "
            "and are flagged RED in conflicts.csv (FR-34).[/bold red]"
        )
    console.print(f"Wrote {run_dir}")


def _load_assignments(path: Path) -> list[Assignment]:
    """Read an assignments.csv back into domain objects (for the FR-44 diff)."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
    return [
        Assignment(
            applicant_id=row["applicant_id"],
            full_name=row["full_name"],
            email=row["email"],
            choice_index=1 if int(row["choice_index"]) == 1 else 2,
            sub_division=row["sub_division"],
            division=DivisionCode(row["division"]),
            panel_id=row["panel_id"],
            room=row["room"],
            slot_id=row["slot_id"],
            date=row["date"],
            start_time=row["start_time"],
            end_time=row["end_time"],
            is_clash=row["is_clash"].lower() == "true",
            is_locked=row["is_locked"].lower() == "true",
            same_parent_pair=row["same_parent_pair"].lower() == "true",
            reason=row["reason"] or None,
        )
        for row in df.to_dict(orient="records")
    ]


# ----------------------------------------------------------------- publish


@app.command()
def publish(
    run: str = typer.Option("latest", "--run", help="Run id under runs/, or 'latest'"),
    input_path: Path = typer.Option(
        Path("data/interim/applicants.clean.csv"),
        "--input",
        help="applicants.clean.csv produced by `iffsched ingest`",
    ),
    runs_dir: Path = typer.Option(Path("runs"), "--runs-dir"),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
    out_dir: Path = typer.Option(
        None, "--out-dir", help="Output directory (default: data/output/<run_id>)"
    ),
    formats: str = typer.Option(
        "xlsx,html", "--formats", help="Comma-separated output formats: xlsx, html"
    ),
) -> None:
    """Publish room, applicant and panel views from a solved run (FR-50..FR-54).

    Conflicts (clashes + capacity warnings) are recomputed from the current
    assignments and applicants and written to `conflicts.csv` alongside the
    views, so publish always reflects what is actually on the grid."""
    run_dir = runs_dir / run
    if not run_dir.exists():
        console.print(f"[red]Run not found: {run_dir}. Run `iffsched solve` first.[/red]")
        raise typer.Exit(code=1)
    assignments_path = run_dir / "assignments.csv"
    if not assignments_path.exists():
        console.print(f"[red]{assignments_path} not found — is this a completed run?[/red]")
        raise typer.Exit(code=1)
    if not input_path.exists():
        console.print(
            f"[red]Input file not found: {input_path}. Run `iffsched ingest` first.[/red]"
        )
        raise typer.Exit(code=1)

    wanted_formats = {f.strip().lower() for f in formats.split(",") if f.strip()}
    unknown_formats = wanted_formats - {"xlsx", "html"}
    if unknown_formats:
        console.print(
            f"[red]Unknown format(s): {sorted(unknown_formats)}. Use xlsx and/or html.[/red]"
        )
        raise typer.Exit(code=1)

    settings = load_settings(config_dir)
    grid = build_slot_grid(settings.event)
    applicants = _load_clean_applicants(input_path)
    panels = resolve_panels(settings.panels, grid)
    rooms = resolve_rooms(settings.rooms)
    assignments = _load_assignments(assignments_path)

    resolved_run_id = run_dir.resolve().name
    publish_dir = out_dir if out_dir is not None else Path("data/output") / resolved_run_id
    publish_dir.mkdir(parents=True, exist_ok=True)

    conflicts = build_conflicts(assignments, applicants, panels)
    _conflicts_frame(conflicts).to_csv(publish_dir / "conflicts.csv", index=False)

    room_views = build_room_views(assignments, panels, rooms, grid.slots)
    applicant_rows = build_applicant_view(assignments)
    panel_views = build_panel_views(assignments, panels, grid.slots)

    if "xlsx" in wanted_formats:
        write_xlsx(
            publish_dir / "schedule.xlsx", room_views, applicant_rows, panel_views, conflicts
        )
    if "html" in wanted_formats:
        html_dir = publish_dir / "html"
        write_room_view_html(room_views, html_dir)
        write_applicant_view_html(applicant_rows, html_dir)
        write_panel_view_html(panel_views, html_dir)

    red = sum(1 for c in conflicts if c.severity == Severity.RED)
    amber = sum(1 for c in conflicts if c.severity == Severity.AMBER)

    table = Table(title=f"Publish {resolved_run_id}")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Room views", str(len(room_views)))
    table.add_row("Applicants", str(len(applicant_rows)))
    table.add_row("Panels", str(len(panel_views)))
    table.add_row("Clashes (RED)", str(red))
    table.add_row("Capacity warnings (AMBER)", str(amber))
    console.print(table)

    if red:
        console.print(
            f"[bold red]{red} clash(es) are marked red in schedule.xlsx (FR-54).[/bold red]"
        )
    console.print(f"Wrote {publish_dir}")


# ---------------------------------------------------------------------- lock


@app.command()
def lock(
    from_path: Path = typer.Option(
        None,
        "--from",
        help="Assignments CSV to pin (e.g. a recruiter-edited runs/latest/assignments.csv). "
        "Validated as a whole before anything is locked (FR-42).",
    ),
    applicant_id: str = typer.Option(
        None, "--applicant", help="Only lock this applicant's rows from --from."
    ),
    locks_path: Path = typer.Option(
        Path("data/locks/pinned_assignments.csv"),
        "--locks",
        help="Where pinned assignments are stored.",
    ),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
    clear: bool = typer.Option(False, "--clear", help="Delete every lock. Requires confirmation."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt for --clear."),
) -> None:
    """Pin manual edits as hard constraints for the next solve (FR-40..FR-42, SPEC.md §11).

    `edit_validator` checks the *entire* incoming file first — a double-booked
    applicant, a busy panel, a room over capacity or a slot outside the grid
    (E-12) — and rejects it with specific messages rather than locking a
    schedule that was never legal. Locks are cumulative: re-locking a choice
    that was already pinned replaces its old pin (SPEC.md §11)."""
    if clear:
        if from_path is not None:
            console.print("[red]--clear cannot be combined with --from.[/red]")
            raise typer.Exit(code=1)
        if not locks_path.exists():
            console.print(f"No locks file at {locks_path}; nothing to clear.")
            return
        existing = _load_locks(locks_path)
        if not yes and not typer.confirm(
            f"Clear all {len(existing)} lock(s) in {locks_path}? This cannot be undone."
        ):
            console.print("Aborted.")
            raise typer.Exit(code=1)
        locks_path.unlink()
        console.print(f"Cleared {len(existing)} lock(s) from {locks_path}.")
        return

    if from_path is None:
        console.print("[red]--from is required (or pass --clear).[/red]")
        raise typer.Exit(code=1)
    if not from_path.exists():
        console.print(f"[red]Assignments file not found: {from_path}[/red]")
        raise typer.Exit(code=1)

    settings = load_settings(config_dir)
    grid = build_slot_grid(settings.event)
    panels = resolve_panels(settings.panels, grid)
    rooms = resolve_rooms(settings.rooms)
    assignments = _load_assignments(from_path)

    violations = validate_edits(assignments, panels, rooms, grid.slots)
    if violations:
        console.print(
            f"[bold red]{len(violations)} illegal edit(s) in {from_path} — nothing was "
            "locked (FR-42, E-12):[/bold red]"
        )
        for violation in violations:
            who = f"{violation.applicant_id}: " if violation.applicant_id else ""
            console.print(f"  [red]{violation.code}[/red] {who}{violation.message}")
        raise typer.Exit(code=1)

    to_lock = assignments
    if applicant_id is not None:
        to_lock = [a for a in assignments if a.applicant_id == applicant_id]
        if not to_lock:
            console.print(f"[red]No rows for applicant '{applicant_id}' in {from_path}.[/red]")
            raise typer.Exit(code=1)

    incoming = [lock_from_assignment(a) for a in to_lock]
    existing = _load_locks(locks_path) if locks_path.exists() else []
    merged = merge_locks(existing, incoming)
    _write_locks(merged, locks_path)

    console.print(f"Locked {len(incoming)} assignment(s) from {from_path}.")
    console.print(f"{locks_path} now holds {len(merged)} pinned assignment(s) in total.")


if __name__ == "__main__":
    app()
