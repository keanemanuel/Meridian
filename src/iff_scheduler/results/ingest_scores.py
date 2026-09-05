"""Turns the committee's raw scoring sheet into validated `DecisionRecord`s
(SPEC.md §10.4, §13 M7: "Score ingestion → decision column").

Mirrors `ingest/validate.py`'s split: pure parse -> pure per-row validation
-> orchestrator that also collapses cross-row problems (here, a duplicate
applicant_id rather than a duplicate email). A blank or unrecognised decision
is *always* rejected to the report, never coerced to REJECTED (CLAUDE.md
invariant 3, SPEC.md §10.4: "a blank must be a hard failure, never a default
to 'rejected'").
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict

from iff_scheduler.domain.enums import Decision, DivisionCode
from iff_scheduler.domain.models import DecisionRecord

Outcome = Literal["REJECTED", "WARNING"]

_DECISION_ALIASES: dict[str, Decision] = {
    "accept": Decision.ACCEPTED,
    "accepted": Decision.ACCEPTED,
    "yes": Decision.ACCEPTED,
    "waitlist": Decision.WAITLIST,
    "wait list": Decision.WAITLIST,
    "wait-list": Decision.WAITLIST,
    "waitlisted": Decision.WAITLIST,
    "reject": Decision.REJECTED,
    "rejected": Decision.REJECTED,
    "no": Decision.REJECTED,
}


def normalize_decision(raw: str) -> Decision | None:
    """Map free-text committee input to a `Decision`. Returns `None` for
    blank or unrecognised text — the caller must treat that as a hard
    failure, never as an implicit REJECTED (SPEC.md §10.4)."""
    return _DECISION_ALIASES.get(raw.strip().lower())


class ScoreValidationReportRow(BaseModel):
    """One line of the scores validation report — mirrors
    `ingest.validate.ValidationReportRow`'s shape for consistency."""

    model_config = ConfigDict(frozen=True)

    row_number: int
    applicant_id: str
    outcome: Outcome
    reason_code: str
    message: str


@dataclass(frozen=True)
class RawScoreRow:
    row_number: int
    applicant_id: str
    raw_decision: str
    raw_division_placed: str


def parse_score_row(raw: Mapping[str, str], row_number: int) -> RawScoreRow:
    return RawScoreRow(
        row_number=row_number,
        applicant_id=(raw.get("applicant_id") or "").strip(),
        raw_decision=(raw.get("decision") or "").strip(),
        raw_division_placed=(raw.get("division_placed") or "").strip(),
    )


def validate_score_row(
    row: RawScoreRow, known_division_codes: set[str]
) -> tuple[list[ScoreValidationReportRow], Decision | None, DivisionCode | None]:
    """Apply every rejection/warning rule to one already-parsed row. Returns
    the issues found plus the decision/division_placed to use if the row is
    otherwise clean — the caller decides whether any REJECTED issue means
    the row is dropped."""
    issues: list[ScoreValidationReportRow] = []

    def add(outcome: Outcome, code: str, message: str) -> None:
        issues.append(
            ScoreValidationReportRow(
                row_number=row.row_number,
                applicant_id=row.applicant_id,
                outcome=outcome,
                reason_code=code,
                message=message,
            )
        )

    if not row.applicant_id:
        add("REJECTED", "MISSING_APPLICANT_ID", f"Row {row.row_number}: applicant_id is blank.")

    decision = normalize_decision(row.raw_decision)
    if decision is None:
        add(
            "REJECTED",
            "BLANK_OR_UNKNOWN_DECISION",
            f"{row.applicant_id or f'row {row.row_number}'}: decision {row.raw_decision!r} is "
            "blank or not recognised — never defaulted to rejected (SPEC.md §10.4).",
        )

    division_placed: DivisionCode | None = None
    if decision == Decision.ACCEPTED:
        if not row.raw_division_placed:
            add(
                "REJECTED",
                "MISSING_DIVISION_PLACED",
                f"{row.applicant_id}: decision is ACCEPTED but division_placed is blank.",
            )
        elif row.raw_division_placed.upper() not in known_division_codes:
            add(
                "REJECTED",
                "UNKNOWN_DIVISION_PLACED",
                f"{row.applicant_id}: division_placed {row.raw_division_placed!r} is not a "
                "known division.",
            )
        else:
            division_placed = DivisionCode(row.raw_division_placed.upper())
    elif row.raw_division_placed:
        add(
            "WARNING",
            "DIVISION_PLACED_IGNORED",
            f"{row.applicant_id}: division_placed is set but decision is "
            f"{row.raw_decision!r}, not ACCEPTED — ignored.",
        )

    return issues, decision, division_placed


@dataclass
class ScoreIngestResult:
    records: list[DecisionRecord]
    report: list[ScoreValidationReportRow]


def run_ingest_scores(
    rows: Iterable[Mapping[str, str]], known_division_codes: set[str]
) -> ScoreIngestResult:
    """Parse and validate every row of the raw scores sheet. A row is dropped
    from `records` if it has any REJECTED issue, an unrecognised decision, a
    blank applicant_id, or a duplicated applicant_id (ambiguous — SPEC.md
    §10.4 treats an unresolvable decision the same as a blank one)."""
    parsed_rows = [parse_score_row(raw, n) for n, raw in enumerate(rows, start=1)]
    id_counts = Counter(r.applicant_id for r in parsed_rows if r.applicant_id)

    report: list[ScoreValidationReportRow] = []
    records: list[DecisionRecord] = []
    for row in parsed_rows:
        issues, decision, division_placed = validate_score_row(row, known_division_codes)
        report.extend(issues)

        is_duplicate = bool(row.applicant_id) and id_counts[row.applicant_id] > 1
        if is_duplicate:
            report.append(
                ScoreValidationReportRow(
                    row_number=row.row_number,
                    applicant_id=row.applicant_id,
                    outcome="REJECTED",
                    reason_code="DUPLICATE_APPLICANT_ID",
                    message=f"{row.applicant_id} appears {id_counts[row.applicant_id]} times in "
                    "the scores file — ambiguous, rejected until resolved.",
                )
            )

        if (
            is_duplicate
            or not row.applicant_id
            or decision is None
            or any(i.outcome == "REJECTED" for i in issues)
        ):
            continue
        records.append(
            DecisionRecord(
                applicant_id=row.applicant_id, decision=decision, division_placed=division_placed
            )
        )

    report.sort(key=lambda r: r.row_number)
    return ScoreIngestResult(records=records, report=report)


DECISIONS_COLUMNS = ["applicant_id", "decision", "division_placed"]
SCORE_REPORT_COLUMNS = ["row_number", "applicant_id", "outcome", "reason_code", "message"]


def read_raw_scores(path: Path) -> list[dict[str, str]]:
    """The one I/O boundary in this module (CLAUDE.md "Architecture rule" —
    `results/` is an adapter layer like `ingest/`, not the pure core)."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False)
    rows: list[dict[str, str]] = df.to_dict(orient="records")  # type: ignore[assignment]
    return rows


def write_score_outputs(result: ScoreIngestResult, clean_path: Path, report_path: Path) -> None:
    """Write decisions.clean.csv and scores_validation_report.csv, mirroring
    `ingest.validate.write_outputs`."""
    clean_rows = [
        {
            "applicant_id": r.applicant_id,
            "decision": r.decision.value,
            "division_placed": r.division_placed.value if r.division_placed else "",
        }
        for r in result.records
    ]
    clean_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(clean_rows, columns=DECISIONS_COLUMNS).to_csv(clean_path, index=False)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_rows = [row.model_dump() for row in result.report]
    pd.DataFrame(report_rows, columns=SCORE_REPORT_COLUMNS).to_csv(report_path, index=False)
