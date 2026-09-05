"""All rejection rules -> validation_report (FR-04, FR-04b, FR-06; SPEC.md §12
E-01b, E-02, E-04). Orchestrates normalize.py end to end: dedupe -> per-row
validation -> clean Applicant list + report.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

import pandas as pd
from pydantic import BaseModel, ConfigDict

from iff_scheduler.domain.grid import SlotGrid
from iff_scheduler.domain.models import Applicant
from iff_scheduler.ingest.base import ApplicantSource
from iff_scheduler.ingest.normalize import ParsedRow, dedupe_by_email, parse_row
from iff_scheduler.settings import DivisionsConfig, EventConfig

# Matches the Google Form's own "tick at least 4 blocks" validation (SPEC.md
# §9.2) — fewer than this doesn't fail ingest, but is worth flagging before
# the solver is forced into a clash for this applicant (E-03).
MIN_AVAILABILITY_SLOTS_WARNING = 4

Outcome = Literal["REJECTED", "COLLAPSED", "WARNING"]


class ValidationReportRow(BaseModel):
    """One line of validation_report.csv — a rejected or suspicious record (FR-06)."""

    model_config = ConfigDict(frozen=True)

    row_number: int
    email: str
    full_name: str
    outcome: Outcome
    reason_code: str
    message: str


@dataclass
class IngestResult:
    applicants: list[Applicant]
    report: list[ValidationReportRow]


def validate_row(row: ParsedRow) -> list[ValidationReportRow]:
    """Apply every rejection/warning rule to one already-parsed row."""
    issues: list[ValidationReportRow] = []

    def add(outcome: Outcome, code: str, message: str) -> None:
        issues.append(
            ValidationReportRow(
                row_number=row.row_number,
                email=row.email,
                full_name=row.full_name,
                outcome=outcome,
                reason_code=code,
                message=message,
            )
        )

    if not row.email:
        add("REJECTED", "MISSING_EMAIL", "Email address is blank.")
    elif "@" not in row.email or row.email.startswith("@") or row.email.endswith("@"):
        add("REJECTED", "INVALID_EMAIL", f"'{row.email}' is not a valid email address.")

    if not row.full_name:
        add("REJECTED", "MISSING_FULL_NAME", "Full name is blank.")

    if row.submitted_at is None:
        add("REJECTED", "INVALID_TIMESTAMP", "Submission timestamp is missing or unparsable.")

    if not row.sub_division_1 or not row.sub_division_2:
        add("REJECTED", "MISSING_SUBDIVISION", "One or both sub-division choices are blank.")
    else:
        if row.division_1 is None:
            add(
                "REJECTED",
                "UNKNOWN_SUBDIVISION",
                f"'{row.sub_division_1}' is not a known sub-division.",
            )
        if row.division_2 is None:
            add(
                "REJECTED",
                "UNKNOWN_SUBDIVISION",
                f"'{row.sub_division_2}' is not a known sub-division.",
            )
        if row.sub_division_1.strip().lower() == row.sub_division_2.strip().lower():
            add(
                "REJECTED",
                "DUPLICATE_SUBDIVISION",
                "First and second choice sub-division must be distinct (SPEC.md E-01b).",
            )

    if not row.availability_slots:
        add(
            "REJECTED",
            "NO_AVAILABILITY",
            "No declared availability overlaps the event slot grid (SPEC.md E-02).",
        )
    elif len(row.availability_slots) < MIN_AVAILABILITY_SLOTS_WARNING:
        add(
            "WARNING",
            "SPARSE_AVAILABILITY",
            f"Only {len(row.availability_slots)} slot(s) available; the solver may be forced "
            "into a clash for this applicant (SPEC.md E-03).",
        )

    return issues


def _collapsed_report_row(row: ParsedRow, kept_row: ParsedRow) -> ValidationReportRow:
    return ValidationReportRow(
        row_number=row.row_number,
        email=row.email,
        full_name=row.full_name,
        outcome="COLLAPSED",
        reason_code="DUPLICATE_EMAIL",
        message=f"Superseded by a later submission from the same email at row "
        f"{kept_row.row_number} (SPEC.md E-04).",
    )


def _duplicate_of_existing_row(row: ParsedRow) -> ValidationReportRow:
    """M10: an incremental batch only sees rows after the watermark, so it
    cannot re-run FR-05's "most recent wins" dedupe against an email that was
    already committed to applicants.clean.csv in a prior run. Rejecting it
    loudly is the invariant-3-safe choice — silently appending a second clean
    row for the same applicant would be a guess about which one is current."""
    return ValidationReportRow(
        row_number=row.row_number,
        email=row.email,
        full_name=row.full_name,
        outcome="REJECTED",
        reason_code="DUPLICATE_OF_EXISTING_APPLICANT",
        message=f"'{row.email}' already has a clean applicant record from a prior ingest run. "
        "Re-run with --force to reprocess the whole sheet.",
    )


def _build_applicant(row: ParsedRow, applicant_id: str) -> Applicant:
    assert row.division_1 is not None
    assert row.division_2 is not None
    assert row.submitted_at is not None
    return Applicant(
        applicant_id=applicant_id,
        full_name=row.full_name,
        email=row.email,
        phone=row.phone,
        sub_division_1=row.sub_division_1,
        sub_division_2=row.sub_division_2,
        division_1=row.division_1,
        division_2=row.division_2,
        availability_slots=row.availability_slots,
        submitted_at=row.submitted_at,
        notes=row.notes,
    )


def run_ingest(
    source: ApplicantSource,
    event: EventConfig,
    divisions: DivisionsConfig,
    grid: SlotGrid,
    *,
    row_number_offset: int = 0,
    applicant_id_offset: int = 0,
    known_emails: frozenset[str] = frozenset(),
) -> IngestResult:
    """`row_number_offset` and `applicant_id_offset` let an incremental sheets
    batch (M10) continue the row-number and applicant-ID sequence of an
    existing applicants.clean.csv rather than restarting at 1 each run.
    `known_emails` are emails already committed from a prior run — see
    `_duplicate_of_existing_row`. All three default to a no-op for the plain
    single-shot CSV path."""
    raw_df = source.read_raw()
    raw_rows = cast("list[dict[str, str]]", raw_df.to_dict(orient="records"))
    parsed = [
        parse_row(raw, row_number_offset + row_number, event, divisions, grid)
        for row_number, raw in enumerate(raw_rows, start=1)
    ]

    kept, collapsed = dedupe_by_email(parsed)
    kept.sort(key=lambda r: r.row_number)
    kept_by_email = {row.email: row for row in kept if row.email}

    report: list[ValidationReportRow] = [
        _collapsed_report_row(row, kept_by_email[row.email])
        for row in collapsed
        if row.email in kept_by_email
    ]

    applicants: list[Applicant] = []
    for row in kept:
        issues = validate_row(row)
        if row.email and row.email in known_emails:
            issues.append(_duplicate_of_existing_row(row))
        report.extend(issues)
        if any(issue.outcome == "REJECTED" for issue in issues):
            continue
        applicants.append(
            _build_applicant(row, applicant_id=f"A{applicant_id_offset + len(applicants) + 1:03d}")
        )

    report.sort(key=lambda r: r.row_number)
    return IngestResult(applicants=applicants, report=report)


CLEAN_COLUMNS = [
    "applicant_id",
    "full_name",
    "email",
    "phone",
    "sub_division_1",
    "sub_division_2",
    "division_1",
    "division_2",
    "availability_slots",
    "submitted_at",
    "notes",
]

REPORT_COLUMNS = ["row_number", "email", "full_name", "outcome", "reason_code", "message"]


def _clean_rows(applicants: list[Applicant]) -> list[dict[str, str]]:
    return [
        {
            "applicant_id": a.applicant_id,
            "full_name": a.full_name,
            "email": a.email,
            "phone": a.phone,
            "sub_division_1": a.sub_division_1,
            "sub_division_2": a.sub_division_2,
            "division_1": a.division_1.value,
            "division_2": a.division_2.value,
            "availability_slots": "|".join(a.availability_slots),
            "submitted_at": a.submitted_at.isoformat(),
            "notes": a.notes or "",
        }
        for a in applicants
    ]


def write_outputs(result: IngestResult, clean_path: Path, report_path: Path) -> None:
    """Write applicants.clean.csv and validation_report.csv (FR-06)."""
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(_clean_rows(result.applicants), columns=CLEAN_COLUMNS).to_csv(
        clean_path, index=False
    )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_rows = [row.model_dump() for row in result.report]
    pd.DataFrame(report_rows, columns=REPORT_COLUMNS).to_csv(report_path, index=False)


def append_outputs(result: IngestResult, clean_path: Path, report_path: Path) -> None:
    """Append one incremental batch's clean applicants and report rows onto
    existing outputs (M10) — the header is written only the first time either
    file is created."""
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    clean_existed = clean_path.exists()
    pd.DataFrame(_clean_rows(result.applicants), columns=CLEAN_COLUMNS).to_csv(
        clean_path, index=False, mode="a", header=not clean_existed
    )

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_existed = report_path.exists()
    report_rows = [row.model_dump() for row in result.report]
    pd.DataFrame(report_rows, columns=REPORT_COLUMNS).to_csv(
        report_path, index=False, mode="a", header=not report_existed
    )
