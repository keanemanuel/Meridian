"""All rejection rules -> validation_report (FR-04, FR-04b, FR-06; SPEC.md §12
E-01b, E-02, E-04). Orchestrates normalize.py end to end: dedupe -> per-row
validation -> clean Applicant list + report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

import pandas as pd
from pydantic import BaseModel, ConfigDict

from iff_scheduler.domain.grid import SlotGrid
from iff_scheduler.domain.models import Applicant
from iff_scheduler.ingest.base import ApplicantSource
from iff_scheduler.ingest.normalize import ParsedRow, dedupe_by_email, parse_row
from iff_scheduler.settings import DivisionsConfig, EventConfig, canonical_sub_division

# Matches the Google Form's own "tick at least 4 blocks" validation (SPEC.md
# §9.2) — fewer than this doesn't fail ingest, but is worth flagging before
# the solver is forced into a clash for this applicant (E-03).
MIN_AVAILABILITY_SLOTS_WARNING = 4

Outcome = Literal["REJECTED", "COLLAPSED", "WARNING"]


class ValidationReportRow(BaseModel):
    """One line of validation_report.csv — a rejected or suspicious record (FR-06)."""

    model_config = ConfigDict(frozen=True)

    row_number: int
    # The row's position in the source CSV/Sheet as a human opening the file
    # sees it: header is line 1, so the first applicant is `csv_row` 2. Lets a
    # committee member jump straight to the offending line to fix it by hand.
    csv_row: int
    email: str
    full_name: str
    sub_division_1: str
    sub_division_2: str
    outcome: Outcome
    reason_code: str
    message: str


# Rejection reasons a human review cannot safely wave through, so the
# "Recover" action in the UI is disabled for them:
#   * a missing/invalid email is genuinely uncontactable — there is no way to
#     send this person their schedule (CLAUDE.md invariant 3);
#   * an unknown or wholly-missing sub-division has no parent division to
#     schedule against, and the pipeline never guesses one.
NON_RECOVERABLE_REASON_CODES = frozenset(
    {
        "MISSING_EMAIL",
        "INVALID_EMAIL",
        "UNKNOWN_SUBDIVISION",
        "MISSING_SUBDIVISION",
        "DUPLICATE_OF_EXISTING_APPLICANT",
    }
)


def is_recoverable(reason_code: str) -> bool:
    """Whether a rejected row can be force-accepted by a human (M-review)."""
    return reason_code not in NON_RECOVERABLE_REASON_CODES


def is_exact_duplicate_pair(sub_division_1: str, sub_division_2: str) -> bool:
    """True when both choices name the *identical* sub-division — e.g. "Program"
    + "Program", "Media Marketing" + "Media Marketing" (SPEC.md E-01b).

    Case and surrounding whitespace are folded, so "Logistics" + " logistics "
    still counts. A same-parent pair of *different* sub-divisions
    (Media Marketing + Media Documentation, Creative + WebMaster) is NOT this:
    those remain two separate interviews (SPEC.md E-01). Comparison is on the
    sub-division text, never the parent division code, so same-parent pairs
    are never collapsed.
    """
    a = canonical_sub_division(sub_division_1)
    b = canonical_sub_division(sub_division_2)
    return bool(a) and a == b


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
                csv_row=row.row_number + 1,
                email=row.email,
                full_name=row.full_name,
                sub_division_1=row.sub_division_1,
                sub_division_2=row.sub_division_2,
                outcome=outcome,
                reason_code=code,
                message=message,
            )
        )

    # A missing or malformed email is uncontactable — still a hard rejection,
    # because there is no address to send the schedule to.
    if not row.email:
        add("REJECTED", "MISSING_EMAIL", "Email address is blank.")
    elif "@" not in row.email or row.email.startswith("@") or row.email.endswith("@"):
        add("REJECTED", "INVALID_EMAIL", f"'{row.email}' is not a valid email address.")

    if not row.full_name:
        add("REJECTED", "MISSING_FULL_NAME", "Full name is blank.")

    if row.submitted_at is None:
        # The timestamp only orders duplicate submissions (FR-05). A blank or
        # unparsable one is not grounds to drop a real registrant, so it warns
        # and falls back to UNKNOWN_SUBMITTED_AT, which loses every dedupe tie
        # to a row that does carry a real timestamp.
        add(
            "WARNING",
            "MISSING_TIMESTAMP",
            "Submission timestamp is missing or unparsable; this row loses any "
            "duplicate-email tie-break against a dated submission.",
        )

    # Sub-division choices. Be maximally accepting: exactly one choice filled
    # is a single-choice applicant (one interview, `single_choice=True` in the
    # clean CSV), not a rejection. Only a row with *both* blank has nothing to
    # schedule at all.
    has_1 = bool(row.sub_division_1.strip())
    has_2 = bool(row.sub_division_2.strip())
    if not has_1 and not has_2:
        add("REJECTED", "MISSING_SUBDIVISION", "Both sub-division choices are blank.")
    else:
        if has_1 and row.division_1 is None:
            add(
                "REJECTED",
                "UNKNOWN_SUBDIVISION",
                f"'{row.sub_division_1}' is not a known sub-division.",
            )
        if has_2 and row.division_2 is None:
            add(
                "REJECTED",
                "UNKNOWN_SUBDIVISION",
                f"'{row.sub_division_2}' is not a known sub-division.",
            )
        # The exact same sub-division picked twice (e.g. "Program" + "Program")
        # is not a rejection: the applicant clearly wants that one role, so it
        # collapses to a single interview, exactly as a single-choice row does
        # (SPEC.md E-01b). A same-parent pair of *different* sub-divisions
        # (Media Marketing + Media Documentation, Creative + WebMaster) is
        # untouched — still two separate interviews (SPEC.md E-01).
        if (
            has_1
            and has_2
            and row.division_1 is not None
            and row.division_2 is not None
            and is_exact_duplicate_pair(row.sub_division_1, row.sub_division_2)
        ):
            add(
                "WARNING",
                "DUPLICATE_SUBDIVISION",
                "Both choices are the identical sub-division; collapsed to a single "
                "interview (SPEC.md E-01b).",
            )

    if not row.availability_slots:
        # E-02 relaxed: rather than dropping an applicant who left the
        # availability question blank, assume they are free for the whole
        # event and flag the assumption (it is filled in by run_ingest).
        add(
            "WARNING",
            "NO_AVAILABILITY",
            "No availability declared — assumed full event availability.",
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
        csv_row=row.row_number + 1,
        email=row.email,
        full_name=row.full_name,
        sub_division_1=row.sub_division_1,
        sub_division_2=row.sub_division_2,
        outcome="COLLAPSED",
        reason_code="DUPLICATE_EMAIL",
        message=f"Superseded by a later submission from the same email at row "
        f"{kept_row.row_number} (SPEC.md E-04).",
    )


def _recovered_report_row(row: ParsedRow, reason_codes: list[str]) -> ValidationReportRow:
    """A row a human chose to force past its rejection (M-review "Recover")."""
    return ValidationReportRow(
        row_number=row.row_number,
        csv_row=row.row_number + 1,
        email=row.email,
        full_name=row.full_name,
        sub_division_1=row.sub_division_1,
        sub_division_2=row.sub_division_2,
        outcome="WARNING",
        reason_code="RECOVERED",
        message="Manually recovered despite: " + ", ".join(sorted(set(reason_codes))) + ".",
    )


def _duplicate_of_existing_row(row: ParsedRow) -> ValidationReportRow:
    """M10: an incremental batch only sees rows after the watermark, so it
    cannot re-run FR-05's "most recent wins" dedupe against an email that was
    already committed to applicants.clean.csv in a prior run. Rejecting it
    loudly is the invariant-3-safe choice — silently appending a second clean
    row for the same applicant would be a guess about which one is current."""
    return ValidationReportRow(
        row_number=row.row_number,
        csv_row=row.row_number + 1,
        email=row.email,
        full_name=row.full_name,
        sub_division_1=row.sub_division_1,
        sub_division_2=row.sub_division_2,
        outcome="REJECTED",
        reason_code="DUPLICATE_OF_EXISTING_APPLICANT",
        message=f"'{row.email}' already has a clean applicant record from a prior ingest run. "
        "Re-run with --force to reprocess the whole sheet.",
    )


# Stand-in for a submission whose timestamp could not be read. Deliberately
# the earliest representable datetime, matching the dedupe tie-break in
# `dedupe_by_email`, and obvious enough in applicants.clean.csv that nobody
# mistakes it for a real submission time (CLAUDE.md invariant 3).
UNKNOWN_SUBMITTED_AT = datetime.min


def _build_applicant(row: ParsedRow, applicant_id: str, all_slot_ids: list[str]) -> Applicant:
    # Normalise a single-choice row so the one real choice is always choice 1
    # — the solver keys spread/balance/C8 off `division_1`, so an applicant
    # whose only pick landed in the "second choice" column still schedules.
    #
    # The identical sub-division picked twice ("Program" + "Program") is
    # treated exactly like a single-choice row: one interview, not two
    # (SPEC.md E-01b). A same-parent pair of different sub-divisions is left
    # as a two-choice applicant (SPEC.md E-01).
    both_filled = bool(row.sub_division_1.strip()) and bool(row.sub_division_2.strip())
    if both_filled and not is_exact_duplicate_pair(row.sub_division_1, row.sub_division_2):
        sub_1, sub_2 = row.sub_division_1, row.sub_division_2
        div_1, div_2 = row.division_1, row.division_2
        single = False
    elif row.sub_division_1.strip():
        sub_1, sub_2 = row.sub_division_1, ""
        div_1, div_2 = row.division_1, None
        single = True
    else:
        sub_1, sub_2 = row.sub_division_2, ""
        div_1, div_2 = row.division_2, None
        single = True

    assert div_1 is not None

    # E-02 relaxed (see validate_row): a blank availability answer means
    # "assume free for the whole event", flagged as a NO_AVAILABILITY warning.
    availability = row.availability_slots or list(all_slot_ids)

    return Applicant(
        applicant_id=applicant_id,
        full_name=row.full_name,
        email=row.email,
        phone=row.phone,
        student_id=row.student_id,
        sub_division_1=sub_1,
        sub_division_2=sub_2,
        division_1=div_1,
        division_2=div_2,
        single_choice=single,
        availability_slots=availability,
        submitted_at=row.submitted_at or UNKNOWN_SUBMITTED_AT,
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
    force_accept_rows: frozenset[int] = frozenset(),
) -> IngestResult:
    """`row_number_offset` and `applicant_id_offset` let an incremental sheets
    batch (M10) continue the row-number and applicant-ID sequence of an
    existing applicants.clean.csv rather than restarting at 1 each run.
    `known_emails` are emails already committed from a prior run — see
    `_duplicate_of_existing_row`.

    `force_accept_rows` is the set of `row_number`s a human chose to recover
    from the validation report: a recoverable rejection (`is_recoverable`) on
    one of these rows is downgraded to a `RECOVERED` warning and the applicant
    is built anyway. A non-recoverable rejection still blocks.

    All default to a no-op for the plain single-shot CSV path."""
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

    all_slot_ids = [slot.slot_id for slot in grid.slots]

    applicants: list[Applicant] = []
    for row in kept:
        issues = validate_row(row)
        if row.email and row.email in known_emails:
            issues.append(_duplicate_of_existing_row(row))

        rejected = [i for i in issues if i.outcome == "REJECTED"]
        forced = row.row_number in force_accept_rows
        blocking = (
            [i for i in rejected if not is_recoverable(i.reason_code)] if forced else rejected
        )
        if blocking:
            # Still rejected — keep the full set of issues in the report.
            report.extend(issues)
            continue

        report.extend(i for i in issues if i.outcome != "REJECTED")
        if forced and rejected:
            report.append(_recovered_report_row(row, [i.reason_code for i in rejected]))

        applicants.append(
            _build_applicant(
                row,
                applicant_id=f"A{applicant_id_offset + len(applicants) + 1:03d}",
                all_slot_ids=all_slot_ids,
            )
        )

    report.sort(key=lambda r: r.row_number)
    return IngestResult(applicants=applicants, report=report)


CLEAN_COLUMNS = [
    "applicant_id",
    "full_name",
    "email",
    "phone",
    "student_id",
    "sub_division_1",
    "sub_division_2",
    "division_1",
    "division_2",
    "single_choice",
    "availability_slots",
    "submitted_at",
    "notes",
]

REPORT_COLUMNS = [
    "row_number",
    "csv_row",
    "email",
    "full_name",
    "sub_division_1",
    "sub_division_2",
    "outcome",
    "reason_code",
    "message",
]


def _clean_rows(applicants: list[Applicant]) -> list[dict[str, str]]:
    return [
        {
            "applicant_id": a.applicant_id,
            "full_name": a.full_name,
            "email": a.email,
            "phone": a.phone,
            "student_id": a.student_id,
            "sub_division_1": a.sub_division_1,
            "sub_division_2": a.sub_division_2,
            "division_1": a.division_1.value,
            "division_2": a.division_2.value if a.division_2 is not None else "",
            "single_choice": "True" if a.single_choice else "False",
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
