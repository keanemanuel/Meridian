"""Command-line interface (SPEC.md §7.1)."""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import cast

import pandas as pd
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from iff_scheduler import workspace as ws
from iff_scheduler.domain.enums import Decision, DivisionCode, SendStatus, Severity
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.domain.models import Applicant, Assignment, Conflict, SendLedgerEntry
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
from iff_scheduler.ingest.validate import IngestResult, append_outputs, run_ingest, write_outputs
from iff_scheduler.notify.audit import (
    audit_invite_recipients,
    audit_result_recipients,
    partition_clash_recipients,
)
from iff_scheduler.notify.base import EmailMessage, SendError
from iff_scheduler.notify.gmail_mailer import GmailMailer
from iff_scheduler.notify.ledger import (
    LEDGER_COLUMNS,
    already_sent,
    ledger_entry_to_row,
    parse_ledger_rows,
    record_attempt,
)
from iff_scheduler.notify.renderer import (
    RenderedEmail,
    build_invite_recipients,
    render_invites,
    render_results,
    write_rendered_emails,
)
from iff_scheduler.results.decide import (
    ResultRecipient,
    build_result_recipients,
    partition_by_decision,
)
from iff_scheduler.results.ingest_scores import (
    read_raw_scores,
    run_ingest_scores,
    write_score_outputs,
)
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
from iff_scheduler.workspace import DEFAULT_WORKSPACE, find_workspace, load_workspaces
from iff_scheduler.workspace import create_workspace as _create_workspace
from iff_scheduler.workspace import set_workspace_sheet as _set_workspace_sheet

app = typer.Typer(add_completion=False, help="IFF recruitment interview scheduler.")
notify_app = typer.Typer(add_completion=False, help="Email notifications (SPEC.md §10).")
workspace_app = typer.Typer(add_completion=False, help="Workspace management (SPEC.md §11).")
app.add_typer(notify_app, name="notify")
app.add_typer(workspace_app, name="workspace")
console = Console()

WORKSPACE_OPTION = typer.Option(DEFAULT_WORKSPACE, "--workspace", help="Isolated data namespace.")

INVITE_TEMPLATE = "invite"


@app.callback()
def _main() -> None:
    """IFF recruitment interview scheduler (SPEC.md §7.1)."""


@workspace_app.command("create")
def workspace_create(
    name: str = typer.Option(..., "--name", help="Unique workspace name, e.g. 'IFF 2026'."),
    group: str = typer.Option(..., "--group", help="Group the workspace belongs to."),
) -> None:
    """Register a new workspace and lay down its data directory (SPEC.md §11.3)."""
    try:
        meta = _create_workspace(name, group)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"Created workspace '{meta.name}' in group '{meta.group}'.")
    console.print(f"Wrote {ws.workspace_root(meta.name)}")


@workspace_app.command("list")
def workspace_list() -> None:
    """Show every registered workspace and its group (SPEC.md §11.3)."""
    workspaces = load_workspaces()
    if not workspaces:
        console.print("No workspaces yet. Run `iffsched workspace create` first.")
        return

    table = Table(title="Workspaces")
    table.add_column("Name")
    table.add_column("Group")
    table.add_column("Sheet ID")
    table.add_column("Created")
    for meta in sorted(workspaces, key=lambda w: (w.group, w.name)):
        table.add_row(meta.name, meta.group, meta.sheet_id or "—", meta.created_at.isoformat())
    console.print(table)


@workspace_app.command("set-sheet")
def workspace_set_sheet(
    workspace: str = typer.Option(..., "--workspace", help="Workspace to update."),
    url: str = typer.Option(..., "--url", help="Google Sheet URL or bare Sheet ID."),
) -> None:
    """Attach a Google Sheet to a workspace as its applicant data source (SPEC.md §11.3)."""
    try:
        meta = _set_workspace_sheet(workspace, url)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print(f"Sheet for '{meta.name}' set to {meta.sheet_id}.")


def _print_ingest_summary(result: IngestResult, clean_path: Path, report_path: Path) -> None:
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


@app.command()
def ingest(
    source: str = typer.Option("csv", "--source", help="Applicant data source: csv|sheets"),
    input_path: Path = typer.Option(
        None, "--input", help="Raw CSV export (required for --source csv)"
    ),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
    out_dir: Path = typer.Option(
        None, "--out-dir", help="Default: data/workspaces/<workspace>/interim"
    ),
    workspace: str = WORKSPACE_OPTION,
    service_account_file: Path = typer.Option(
        None,
        "--service-account",
        help="Overrides GOOGLE_SERVICE_ACCOUNT_FILE from .env (--source sheets)",
    ),
    worksheet: str = typer.Option(
        None, "--worksheet", help="Worksheet/tab name (--source sheets; default: first sheet)"
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-process the whole Sheet from scratch, ignoring the watermark (--source sheets)",
    ),
) -> None:
    """Ingest applicant data, normalise it, and write a validation report (FR-01..FR-07).

    `--source csv` is a one-shot full read of a manual export. `--source
    sheets` reads the workspace's linked Google Sheet incrementally: only
    rows after the watermark in `last_ingested_row.txt` are processed and the
    result is appended to the existing applicants.clean.csv (FR-07, M10).
    `--force` discards the watermark and re-ingests the entire Sheet,
    overwriting the outputs instead of appending.
    """
    resolved_out_dir = out_dir if out_dir is not None else ws.interim_dir(workspace)
    clean_path = resolved_out_dir / "applicants.clean.csv"
    report_path = resolved_out_dir / "validation_report.csv"

    settings = load_settings(config_dir)
    grid = build_slot_grid(settings.event)

    if source == "csv":
        if input_path is None:
            console.print("[red]--input is required when --source csv[/red]")
            raise typer.Exit(code=1)
        if not input_path.exists():
            console.print(f"[red]Input file not found: {input_path}[/red]")
            raise typer.Exit(code=1)

        result = run_ingest(
            source=CsvApplicantSource(path=input_path),
            event=settings.event,
            divisions=settings.divisions,
            grid=grid,
        )
        write_outputs(result, clean_path=clean_path, report_path=report_path)
        _print_ingest_summary(result, clean_path, report_path)
        return

    if source != "sheets":
        console.print(f"[red]Source '{source}' is not implemented. Use --source csv|sheets.[/red]")
        raise typer.Exit(code=1)

    meta = find_workspace(workspace, load_workspaces())
    if meta is None or meta.sheet_id is None:
        console.print(
            f"[red]Workspace '{workspace}' has no Sheet attached. Run "
            "`iffsched workspace set-sheet --workspace ... --url ...` first.[/red]"
        )
        raise typer.Exit(code=1)

    load_dotenv()
    sa_file = (
        str(service_account_file)
        if service_account_file is not None
        else os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    )
    if not sa_file:
        console.print(
            "[red]No service account file. Pass --service-account or set "
            "GOOGLE_SERVICE_ACCOUNT_FILE in .env.[/red]"
        )
        raise typer.Exit(code=1)

    worksheet_handle = open_worksheet(sa_file, meta.sheet_id, worksheet)
    sheets_source = SheetsApplicantSource(worksheet=worksheet_handle)
    watermark_path = ws.last_ingested_row_path(workspace)

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
        console.print("No new rows since the last ingest. Nothing to do.")
        return

    if force:
        write_outputs(incremental.result, clean_path=clean_path, report_path=report_path)
    else:
        append_outputs(incremental.result, clean_path=clean_path, report_path=report_path)
    write_watermark(watermark_path, incremental.watermark_after)

    _print_ingest_summary(incremental.result, clean_path, report_path)
    console.print(
        f"Watermark {incremental.watermark_before} -> {incremental.watermark_after} "
        f"({incremental.new_row_count} new row(s) read)."
    )


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
        None,
        "--input",
        help="applicants.clean.csv produced by `iffsched ingest` "
        "(default: data/workspaces/<workspace>/interim/applicants.clean.csv)",
    ),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
    workspace: str = WORKSPACE_OPTION,
) -> None:
    """Capacity Advisor — demand vs. panel-slot supply per division (SPEC.md §5.5).

    Hard-stops with exit code 1 if any division is INFEASIBLE, so a bad
    panel/room configuration is caught before the solver runs (E-06)."""
    resolved_input_path = (
        input_path if input_path is not None else ws.applicants_clean_path(workspace)
    )
    if not resolved_input_path.exists():
        console.print(
            f"[red]Input file not found: {resolved_input_path}. Run `iffsched ingest` first.[/red]"
        )
        raise typer.Exit(code=1)

    settings = load_settings(config_dir)
    grid = build_slot_grid(settings.event)
    applicants = _load_clean_applicants(resolved_input_path)

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
        None,
        "--input",
        help="applicants.clean.csv produced by `iffsched ingest` "
        "(default: data/workspaces/<workspace>/interim/applicants.clean.csv)",
    ),
    solver: str = typer.Option("cpsat", "--solver", help="Solver to use: cpsat|greedy"),
    locks_path: Path = typer.Option(
        None,
        "--locks",
        help="Pinned assignments the solver must honour (C6). Ignored if absent. "
        "(default: data/workspaces/<workspace>/locks/pinned_assignments.csv)",
    ),
    runs_dir: Path = typer.Option(
        None, "--runs-dir", help="Default: data/workspaces/<workspace>/runs"
    ),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
    skip_check: bool = typer.Option(
        False, "--skip-check", help="Solve even if the Capacity Advisor says INFEASIBLE (E-06)."
    ),
    workspace: str = WORKSPACE_OPTION,
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

    resolved_input_path = (
        input_path if input_path is not None else ws.applicants_clean_path(workspace)
    )
    resolved_locks_path = locks_path if locks_path is not None else ws.locks_path(workspace)
    resolved_runs_dir = runs_dir if runs_dir is not None else ws.runs_dir(workspace)

    if not resolved_input_path.exists():
        console.print(
            f"[red]Input file not found: {resolved_input_path}. Run `iffsched ingest` first.[/red]"
        )
        raise typer.Exit(code=1)

    settings = load_settings(config_dir)
    grid = build_slot_grid(settings.event)
    applicants = _load_clean_applicants(resolved_input_path)

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
    if resolved_locks_path.exists():
        locks = _load_locks(resolved_locks_path)
        console.print(
            f"Honouring {len(locks)} locked assignment(s) from {resolved_locks_path} (C6)."
        )

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

    run_dir = resolved_runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    previous_dir = _previous_run(resolved_runs_dir, run_dir)

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

    _point_latest_at(resolved_runs_dir, run_dir)

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
        None,
        "--input",
        help="applicants.clean.csv produced by `iffsched ingest` "
        "(default: data/workspaces/<workspace>/interim/applicants.clean.csv)",
    ),
    runs_dir: Path = typer.Option(
        None, "--runs-dir", help="Default: data/workspaces/<workspace>/runs"
    ),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
    out_dir: Path = typer.Option(
        None,
        "--out-dir",
        help="Output directory (default: data/workspaces/<workspace>/output/<run_id>)",
    ),
    formats: str = typer.Option(
        "xlsx,html", "--formats", help="Comma-separated output formats: xlsx, html"
    ),
    workspace: str = WORKSPACE_OPTION,
) -> None:
    """Publish room, applicant and panel views from a solved run (FR-50..FR-54).

    Conflicts (clashes + capacity warnings) are recomputed from the current
    assignments and applicants and written to `conflicts.csv` alongside the
    views, so publish always reflects what is actually on the grid."""
    resolved_input_path = (
        input_path if input_path is not None else ws.applicants_clean_path(workspace)
    )
    resolved_runs_dir = runs_dir if runs_dir is not None else ws.runs_dir(workspace)

    run_dir = resolved_runs_dir / run
    if not run_dir.exists():
        console.print(f"[red]Run not found: {run_dir}. Run `iffsched solve` first.[/red]")
        raise typer.Exit(code=1)
    assignments_path = run_dir / "assignments.csv"
    if not assignments_path.exists():
        console.print(f"[red]{assignments_path} not found — is this a completed run?[/red]")
        raise typer.Exit(code=1)
    if not resolved_input_path.exists():
        console.print(
            f"[red]Input file not found: {resolved_input_path}. Run `iffsched ingest` first.[/red]"
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
    applicants = _load_clean_applicants(resolved_input_path)
    panels = resolve_panels(settings.panels, grid)
    rooms = resolve_rooms(settings.rooms)
    assignments = _load_assignments(assignments_path)

    resolved_run_id = run_dir.resolve().name
    publish_dir = out_dir if out_dir is not None else ws.output_dir(workspace) / resolved_run_id
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
        None,
        "--locks",
        help="Where pinned assignments are stored "
        "(default: data/workspaces/<workspace>/locks/pinned_assignments.csv)",
    ),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
    clear: bool = typer.Option(False, "--clear", help="Delete every lock. Requires confirmation."),
    yes: bool = typer.Option(False, "--yes", help="Skip the confirmation prompt for --clear."),
    workspace: str = WORKSPACE_OPTION,
) -> None:
    """Pin manual edits as hard constraints for the next solve (FR-40..FR-42, SPEC.md §11).

    `edit_validator` checks the *entire* incoming file first — a double-booked
    applicant, a busy panel, a room over capacity or a slot outside the grid
    (E-12) — and rejects it with specific messages rather than locking a
    schedule that was never legal. Locks are cumulative: re-locking a choice
    that was already pinned replaces its old pin (SPEC.md §11)."""
    locks_path = locks_path if locks_path is not None else ws.locks_path(workspace)
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


# --------------------------------------------------------------- notify invite


def _load_ledger(path: Path) -> list[SendLedgerEntry]:
    """Read the send ledger (FR-62). Missing file means nobody has been sent
    to yet — not an error."""
    if not path.exists():
        return []
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
    rows = cast("list[dict[str, str]]", df.to_dict(orient="records"))
    try:
        return parse_ledger_rows(rows)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _write_ledger(entries: list[SendLedgerEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [ledger_entry_to_row(e) for e in sorted(entries, key=lambda e: e.ledger_id)]
    pd.DataFrame(rows, columns=LEDGER_COLUMNS).to_csv(path, index=False)


@notify_app.command("invite")
def notify_invite(
    run: str = typer.Option("latest", "--run", help="Run id under runs/, or 'latest'"),
    input_path: Path = typer.Option(
        None,
        "--input",
        help="applicants.clean.csv produced by `iffsched ingest` "
        "(default: data/workspaces/<workspace>/interim/applicants.clean.csv)",
    ),
    runs_dir: Path = typer.Option(
        None, "--runs-dir", help="Default: data/workspaces/<workspace>/runs"
    ),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
    out_dir: Path = typer.Option(
        None, "--out-dir", help="Where rendered emails are written (default: <run>/emails)"
    ),
    ledger_path: Path = typer.Option(
        None,
        "--ledger",
        help="Send ledger (FR-62). Default: data/workspaces/<workspace>/ledger/send_ledger.csv",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Render every email to disk. Sends nothing (FR-63)."
    ),
    send: bool = typer.Option(
        False, "--send", help="Actually send. Requires typed confirmation (FR-64)."
    ),
    service_account_file: Path = typer.Option(
        None, "--service-account", help="Overrides GOOGLE_SERVICE_ACCOUNT_FILE from .env"
    ),
    workspace: str = WORKSPACE_OPTION,
) -> None:
    """Send invite emails for a solved run (FR-60..FR-64, SPEC.md §10.2, §10.3).

    Exactly one of --dry-run or --send is required. Both render every email
    first and run the pre-send audit (FR-64) — any blank merge field,
    invalid/duplicate address, or applicant missing an assignment hard-fails
    the whole batch, nothing is sent. Clash assignments (FR-34) are held
    back from the automated send and listed separately for manual, personal
    sending (SPEC.md §10.3). --send additionally requires typing the
    expected recipient count and checks the ledger for idempotency (FR-62)."""
    if dry_run == send:
        console.print("[red]Pass exactly one of --dry-run or --send.[/red]")
        raise typer.Exit(code=1)

    input_path = input_path if input_path is not None else ws.applicants_clean_path(workspace)
    runs_dir = runs_dir if runs_dir is not None else ws.runs_dir(workspace)
    ledger_path = ledger_path if ledger_path is not None else ws.send_ledger_path(workspace)

    run_dir = runs_dir / run
    assignments_path = run_dir / "assignments.csv"
    if not assignments_path.exists():
        console.print(f"[red]{assignments_path} not found. Run `iffsched solve` first.[/red]")
        raise typer.Exit(code=1)
    if not input_path.exists():
        console.print(
            f"[red]Input file not found: {input_path}. Run `iffsched ingest` first.[/red]"
        )
        raise typer.Exit(code=1)

    settings = load_settings(config_dir)
    assignments = _load_assignments(assignments_path)
    resolved_run_id = run_dir.resolve().name

    recipients = build_invite_recipients(assignments, settings.divisions, settings.event)

    issues = audit_invite_recipients(recipients)
    if issues:
        console.print(
            f"[bold red]{len(issues)} audit issue(s) — nothing rendered or sent (FR-64):[/bold red]"
        )
        for issue in issues:
            console.print(f"  [red]{issue.code}[/red] {issue.applicant_id}: {issue.message}")
        raise typer.Exit(code=1)

    auto, manual = partition_clash_recipients(recipients)
    rendered_auto = render_invites(auto, settings.event, settings.notify)
    rendered_manual = render_invites(manual, settings.event, settings.notify)

    emails_dir = out_dir if out_dir is not None else run_dir / "emails"
    write_rendered_emails(rendered_auto, emails_dir)
    if manual:
        write_rendered_emails(rendered_manual, emails_dir / "manual_review")
        pd.DataFrame(
            [
                {"applicant_id": r.applicant_id, "full_name": r.full_name, "email": r.email}
                for r in manual
            ]
        ).to_csv(emails_dir / "manual_review.csv", index=False)

    table = Table(title=f"Notify invite — {resolved_run_id}")
    table.add_column("Metric")
    table.add_column("Count", justify="right")
    table.add_row("Total applicants", str(len(recipients)))
    table.add_row("Auto-sendable", str(len(auto)))
    table.add_row("Held for manual send (clash)", str(len(manual)))
    console.print(table)

    if manual:
        console.print(
            f"[yellow]{len(manual)} applicant(s) have a clash assignment (FR-34) and are held "
            f"back for manual, personal sending — see {emails_dir / 'manual_review.csv'} "
            "(SPEC.md §10.3).[/yellow]"
        )

    if dry_run:
        console.print(
            f"Rendered {len(rendered_auto) + len(rendered_manual)} email(s) to {emails_dir} "
            "(dry run — nothing sent)."
        )
        for email in rendered_auto[:3]:
            console.print(
                f"\n[bold]Sample — {email.applicant_id} <{email.to_email}>[/bold]\n"
                f"Subject: {email.subject}\n{email.text_body}"
            )
        return

    # --send
    ledger = _load_ledger(ledger_path)
    pending = [r for r in auto if not already_sent(ledger, r.applicant_id, INVITE_TEMPLATE)]
    already_sent_count = len(auto) - len(pending)

    if not pending:
        console.print(
            f"All {len(auto)} auto-sendable applicant(s) are already SENT in {ledger_path}. "
            "Nothing to do."
        )
        return

    console.print(f"{already_sent_count} already sent, {len(pending)} pending.")
    _confirm_recipient_count(len(pending))

    mailer = _build_gmail_mailer(settings, service_account_file)

    rendered_by_applicant = {email.applicant_id: email for email in rendered_auto}
    ledger = _send_batch(
        mailer=mailer,
        pending=[(r.applicant_id, r.email, INVITE_TEMPLATE) for r in pending],
        rendered_by_applicant=rendered_by_applicant,
        ledger=ledger,
        ledger_path=ledger_path,
        run_id=resolved_run_id,
        throttle_seconds=settings.notify.throttle_seconds,
        retry_hint="notify invite --send",
    )


def _confirm_recipient_count(expected_count: int) -> None:
    """The FR-64 typed-confirmation gate, shared by every `notify ... --send`
    command: `--send` alone is never enough."""
    expected = typer.prompt(
        f"Type the number of recipients to confirm sending ({expected_count} expected)", type=int
    )
    if expected != expected_count:
        console.print(
            f"[red]Confirmation count {expected} does not match {expected_count} pending "
            "recipient(s). Aborted — nothing was sent.[/red]"
        )
        raise typer.Exit(code=1)


def _build_gmail_mailer(settings: Settings, service_account_file: Path | None) -> GmailMailer:
    load_dotenv()
    sa_file = (
        str(service_account_file)
        if service_account_file is not None
        else os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    )
    sender_email = settings.notify.sender_email or os.environ.get("GMAIL_SENDER_EMAIL", "")
    if not sa_file:
        console.print(
            "[red]No service account file: pass --service-account or set "
            "GOOGLE_SERVICE_ACCOUNT_FILE in .env.[/red]"
        )
        raise typer.Exit(code=1)
    if not sender_email:
        console.print(
            "[red]No sender email: set sender_email in config/notify.yaml or "
            "GMAIL_SENDER_EMAIL in .env.[/red]"
        )
        raise typer.Exit(code=1)

    return GmailMailer(
        service_account_file=sa_file,
        sender_email=sender_email,
        sender_name=settings.notify.sender_name,
    )


def _send_batch(
    mailer: GmailMailer,
    pending: list[tuple[str, str, str]],
    rendered_by_applicant: dict[str, RenderedEmail],
    ledger: list[SendLedgerEntry],
    ledger_path: Path,
    run_id: str,
    throttle_seconds: float,
    retry_hint: str,
) -> list[SendLedgerEntry]:
    """The send loop shared by `notify invite --send` and `notify result
    --send` (SPEC.md §10.2 steps 5-6): ledger check already happened by the
    time `pending` is built, so this just sends, records every attempt
    immediately, and keeps going past a failure (FR-66)."""
    sent_count = 0
    failed: list[str] = []
    for applicant_id, email_address, template in pending:
        email = rendered_by_applicant[applicant_id]
        try:
            provider_message_id = mailer.send(
                EmailMessage(
                    to_email=email.to_email,
                    to_name=email.to_name,
                    subject=email.subject,
                    html_body=email.html_body,
                    text_body=email.text_body,
                )
            )
        except SendError as exc:
            ledger = record_attempt(
                ledger,
                applicant_id=applicant_id,
                email=email_address,
                template=template,
                run_id=run_id,
                status=SendStatus.FAILED,
                error=str(exc),
            )
            failed.append(applicant_id)
            console.print(f"  [red]FAILED[/red] {applicant_id} <{email_address}>: {exc}")
        else:
            ledger = record_attempt(
                ledger,
                applicant_id=applicant_id,
                email=email_address,
                template=template,
                run_id=run_id,
                status=SendStatus.SENT,
                provider_message_id=provider_message_id,
                sent_at=datetime.now(),
            )
            sent_count += 1
            console.print(f"  [green]SENT[/green] {applicant_id} <{email_address}>")
        # Written immediately after every attempt (SPEC.md §10.2 step 5) — a
        # crash mid-batch leaves every prior send recorded, so a re-run skips
        # them via `already_sent` instead of double-sending.
        _write_ledger(ledger, ledger_path)
        time.sleep(throttle_seconds)

    console.print(f"Sent {sent_count}/{len(pending)}. Failed: {len(failed)}.")
    if failed:
        console.print(
            f"[bold red]Failed applicant(s): {', '.join(failed)} — re-run `{retry_hint}` to "
            "retry; the ledger keeps their FAILED rows (FR-66).[/bold red]"
        )
    return ledger


# --------------------------------------------------------------- notify result

RESULT_LIST_COLUMNS = ["applicant_id", "full_name", "email", "decision", "division_placed"]


def _write_result_review_lists(
    recipients: list[ResultRecipient], out_dir: Path
) -> dict[Decision, Path]:
    """One CSV per decision (SPEC.md §10.4: "Have a second person eyeball the
    accepted list and the rejected list separately before approval") —
    written on every dry-run so there is always a current list to check
    before `--send --verified-by` is accepted."""
    out_dir.mkdir(parents=True, exist_ok=True)
    grouped = partition_by_decision(recipients)
    paths: dict[Decision, Path] = {}
    for decision, group in grouped.items():
        path = out_dir / f"{decision.value.lower()}_list.csv"
        rows = [
            {
                "applicant_id": r.applicant_id,
                "full_name": r.full_name,
                "email": r.email,
                "decision": r.decision.value,
                "division_placed": r.division_placed_display or "",
            }
            for r in sorted(group, key=lambda r: r.applicant_id)
        ]
        pd.DataFrame(rows, columns=RESULT_LIST_COLUMNS).to_csv(path, index=False)
        paths[decision] = path
    return paths


VERIFICATION_LOG_COLUMNS = [
    "timestamp",
    "run_id",
    "verified_by",
    "total",
    "accepted",
    "waitlist",
    "rejected",
]


def _append_verification_log(
    log_path: Path,
    run_id: str,
    verified_by: str,
    grouped: dict[Decision, list[ResultRecipient]],
) -> None:
    """Auditable record of who did the second-person check required by
    SPEC.md §10.4, and when — so the sign-off survives after the terminal
    session that ran `--send` is gone."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        pd.read_csv(log_path, dtype=str, keep_default_na=False) if log_path.exists() else None
    )
    row = pd.DataFrame(
        [
            {
                "timestamp": datetime.now().isoformat(),
                "run_id": run_id,
                "verified_by": verified_by,
                "total": str(sum(len(g) for g in grouped.values())),
                "accepted": str(len(grouped[Decision.ACCEPTED])),
                "waitlist": str(len(grouped[Decision.WAITLIST])),
                "rejected": str(len(grouped[Decision.REJECTED])),
            }
        ],
        columns=VERIFICATION_LOG_COLUMNS,
    )
    combined = pd.concat([existing, row], ignore_index=True) if existing is not None else row
    combined.to_csv(log_path, index=False)


@notify_app.command("result")
def notify_result(
    scores_path: Path = typer.Option(
        None,
        "--scores",
        help="Committee scoring sheet: columns applicant_id, decision, division_placed "
        "(division_placed required only when decision is ACCEPTED). "
        "Default: data/workspaces/<workspace>/raw/scores.csv",
    ),
    run: str = typer.Option("latest", "--run", help="Run id under runs/, or 'latest'"),
    input_path: Path = typer.Option(
        None,
        "--input",
        help="applicants.clean.csv produced by `iffsched ingest` "
        "(default: data/workspaces/<workspace>/interim/applicants.clean.csv)",
    ),
    runs_dir: Path = typer.Option(
        None, "--runs-dir", help="Default: data/workspaces/<workspace>/runs"
    ),
    config_dir: Path = typer.Option(DEFAULT_CONFIG_DIR, "--config-dir"),
    out_dir: Path = typer.Option(
        None,
        "--out-dir",
        help="Where rendered emails and review lists are written (default: <run>/results_emails)",
    ),
    ledger_path: Path = typer.Option(
        None,
        "--ledger",
        help="Send ledger (FR-62). Default: data/workspaces/<workspace>/ledger/send_ledger.csv",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Render every email and review list to disk. Sends nothing."
    ),
    send: bool = typer.Option(
        False, "--send", help="Actually send. Requires --verified-by and typed confirmation."
    ),
    verified_by: str = typer.Option(
        None,
        "--verified-by",
        help="Name of the second person who independently checked accepted_list.csv and "
        "rejected_list.csv against the committee's records (required for --send, SPEC.md §10.4).",
    ),
    service_account_file: Path = typer.Option(
        None, "--service-account", help="Overrides GOOGLE_SERVICE_ACCOUNT_FILE from .env"
    ),
    workspace: str = WORKSPACE_OPTION,
) -> None:
    """Send result emails for a completed round (FR-65, SPEC.md §10.4).

    Every applicant in `--input` must have exactly one decision in
    `--scores`; a blank, unrecognised or missing decision hard-fails the
    whole batch before anything is rendered (CLAUDE.md invariant 3 — never
    defaulted to REJECTED). Routes each applicant to the accepted/waitlist/
    rejected template by their decision, reusing the same ledger, audit and
    send-loop machinery as `notify invite` (M6). `--send` additionally
    requires `--verified-by <name>`: a second person must have checked
    `accepted_list.csv` and `rejected_list.csv` (written by `--dry-run`)
    before a send is accepted."""
    if dry_run == send:
        console.print("[red]Pass exactly one of --dry-run or --send.[/red]")
        raise typer.Exit(code=1)

    scores_path = scores_path if scores_path is not None else ws.scores_path(workspace)
    input_path = input_path if input_path is not None else ws.applicants_clean_path(workspace)
    runs_dir = runs_dir if runs_dir is not None else ws.runs_dir(workspace)
    ledger_path = ledger_path if ledger_path is not None else ws.send_ledger_path(workspace)

    run_dir = runs_dir / run
    if not run_dir.exists():
        console.print(f"[red]Run not found: {run_dir}. Run `iffsched solve` first.[/red]")
        raise typer.Exit(code=1)
    if not input_path.exists():
        console.print(
            f"[red]Input file not found: {input_path}. Run `iffsched ingest` first.[/red]"
        )
        raise typer.Exit(code=1)
    if not scores_path.exists():
        console.print(
            f"[red]Scores file not found: {scores_path}. Export the committee's scoring sheet "
            "there first (columns: applicant_id, decision, division_placed).[/red]"
        )
        raise typer.Exit(code=1)

    settings = load_settings(config_dir)
    applicants = _load_clean_applicants(input_path)
    resolved_run_id = run_dir.resolve().name
    emails_dir = out_dir if out_dir is not None else run_dir / "results_emails"

    known_division_codes = {code.value for code in DivisionCode}
    score_result = run_ingest_scores(read_raw_scores(scores_path), known_division_codes)
    for warning in (r for r in score_result.report if r.outcome == "WARNING"):
        console.print(f"  [yellow]{warning.reason_code}[/yellow] {warning.message}")

    rejected_scores = [r for r in score_result.report if r.outcome == "REJECTED"]
    if rejected_scores:
        emails_dir.mkdir(parents=True, exist_ok=True)
        report_path = emails_dir / "scores_validation_report.csv"
        write_score_outputs(score_result, emails_dir / "decisions.clean.csv", report_path)
        console.print(
            f"[bold red]{len(rejected_scores)} row(s) in {scores_path} were rejected — nothing "
            f"rendered or sent (SPEC.md §10.4). See {report_path}:[/bold red]"
        )
        for row in rejected_scores:
            console.print(f"  [red]{row.reason_code}[/red] {row.applicant_id}: {row.message}")
        raise typer.Exit(code=1)

    recipients, decision_issues = build_result_recipients(
        applicants, score_result.records, settings.divisions
    )
    if decision_issues:
        console.print(
            f"[bold red]{len(decision_issues)} decision issue(s) — nothing rendered or sent "
            "(SPEC.md §10.4):[/bold red]"
        )
        for decision_issue in decision_issues:
            console.print(
                f"  [red]{decision_issue.code}[/red] "
                f"{decision_issue.applicant_id}: {decision_issue.message}"
            )
        raise typer.Exit(code=1)

    audit_issues = audit_result_recipients(recipients, settings.notify)
    if audit_issues:
        console.print(
            f"[bold red]{len(audit_issues)} audit issue(s) — nothing rendered or sent "
            "(SPEC.md §10.4):[/bold red]"
        )
        for issue in audit_issues:
            console.print(f"  [red]{issue.code}[/red] {issue.applicant_id}: {issue.message}")
        raise typer.Exit(code=1)

    rendered = render_results(recipients, settings.event, settings.notify)
    write_rendered_emails(rendered, emails_dir)
    list_paths = _write_result_review_lists(recipients, emails_dir)
    grouped = partition_by_decision(recipients)

    table = Table(title=f"Notify result — {resolved_run_id}")
    table.add_column("Decision")
    table.add_column("Count", justify="right")
    for decision in Decision:
        table.add_row(decision.value, str(len(grouped[decision])))
    console.print(table)

    if dry_run:
        console.print(
            f"Rendered {len(rendered)} email(s) to {emails_dir} (dry run — nothing sent)."
        )
        console.print(
            "Before sending: have a second person independently check "
            f"{list_paths[Decision.ACCEPTED]} and {list_paths[Decision.REJECTED]} against the "
            "committee's records (SPEC.md §10.4), then re-run with "
            '`--send --verified-by "Their Name"`.'
        )
        rendered_by_decision = {r.applicant_id: r.decision for r in recipients}
        seen: set[Decision] = set()
        for email in rendered:
            decision = rendered_by_decision[email.applicant_id]
            if decision in seen:
                continue
            seen.add(decision)
            console.print(
                f"\n[bold]Sample ({decision.value}) — {email.applicant_id} <{email.to_email}>"
                f"[/bold]\nSubject: {email.subject}\n{email.text_body}"
            )
        return

    # --send
    if not verified_by or not verified_by.strip():
        console.print(
            "[bold red]--verified-by is required for --send (SPEC.md §10.4). Have a second "
            f"person check {list_paths[Decision.ACCEPTED]} and {list_paths[Decision.REJECTED]} "
            'first, then re-run with --send --verified-by "Their Name". Nothing was sent.'
            "[/bold red]"
        )
        raise typer.Exit(code=1)

    # Audit twice, once before the verification gate and once here, right
    # before send — SPEC.md §10.4: "Run the audit twice. A merge-field error
    # here means telling someone the wrong outcome."
    audit_issues = audit_result_recipients(recipients, settings.notify)
    if audit_issues:
        console.print(
            f"[bold red]{len(audit_issues)} audit issue(s) on the second pass — nothing sent."
            "[/bold red]"
        )
        for issue in audit_issues:
            console.print(f"  [red]{issue.code}[/red] {issue.applicant_id}: {issue.message}")
        raise typer.Exit(code=1)

    ledger = _load_ledger(ledger_path)
    pending = [r for r in recipients if not already_sent(ledger, r.applicant_id, r.template_name)]
    already_sent_count = len(recipients) - len(pending)

    if not pending:
        console.print(
            f"All {len(recipients)} applicant(s) already have a SENT result in {ledger_path}. "
            "Nothing to do."
        )
        return

    console.print(f"{already_sent_count} already sent, {len(pending)} pending.")
    _confirm_recipient_count(len(pending))

    _append_verification_log(
        emails_dir / "verification_log.csv", resolved_run_id, verified_by.strip(), grouped
    )
    console.print(f"Recorded verification by {verified_by.strip()!r} for this batch.")

    mailer = _build_gmail_mailer(settings, service_account_file)
    rendered_by_applicant = {email.applicant_id: email for email in rendered}
    _send_batch(
        mailer=mailer,
        pending=[(r.applicant_id, r.email, r.template_name) for r in pending],
        rendered_by_applicant=rendered_by_applicant,
        ledger=ledger,
        ledger_path=ledger_path,
        run_id=resolved_run_id,
        throttle_seconds=settings.notify.throttle_seconds,
        retry_hint="notify result --send",
    )


if __name__ == "__main__":
    app()
