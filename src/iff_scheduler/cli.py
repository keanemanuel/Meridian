"""Command-line interface (SPEC.md §7.1)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.domain.models import Applicant, Assignment
from iff_scheduler.ingest.csv_source import CsvApplicantSource
from iff_scheduler.ingest.validate import run_ingest, write_outputs
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
    required = {"applicant_id", "choice_index", "panel_id", "slot_id"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{path}: locks file is missing column(s) {sorted(missing)}")
    locks: list[Lock] = []
    for row in df.to_dict(orient="records"):
        choice_index = int(row["choice_index"])
        if choice_index not in (1, 2):
            raise ValueError(
                f"{path}: choice_index must be 1 or 2, got {row['choice_index']!r} "
                f"for applicant {row['applicant_id']!r}"
            )
        locks.append(
            Lock(
                applicant_id=row["applicant_id"],
                choice_index=1 if choice_index == 1 else 2,
                panel_id=row["panel_id"],
                slot_id=row["slot_id"],
            )
        )
    return locks


def _assignments_frame(assignments: list[Assignment]) -> pd.DataFrame:
    rows = [
        {
            **a.model_dump(mode="json"),
        }
        for a in assignments
    ]
    return pd.DataFrame(rows, columns=ASSIGNMENT_COLUMNS)


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
    pd.DataFrame(
        [c.model_dump(mode="json") for c in conflicts],
        columns=["applicant_id", "severity", "type", "message"],
    ).to_csv(run_dir / "conflicts.csv", index=False)
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


if __name__ == "__main__":
    app()
