"""Tests for M7 results — ingest_scores.py (SPEC.md §10.4, §13 M7).

CLAUDE.md invariant 3: "Bad or ambiguous input is rejected to a validation
report... never assumes a missing decision means rejection." Every test here
checks that a blank/unknown decision is REJECTED to the report, not silently
coerced.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from iff_scheduler.domain.enums import Decision, DivisionCode
from iff_scheduler.results.ingest_scores import (
    RawScoreRow,
    normalize_decision,
    parse_score_row,
    run_ingest_scores,
    validate_score_row,
    write_score_outputs,
)

KNOWN_DIVISIONS = {d.value for d in DivisionCode}


def _row(**overrides: str) -> dict[str, str]:
    base = {"applicant_id": "A001", "decision": "Accepted", "division_placed": "CREATIVE"}
    base.update(overrides)
    return base


# ------------------------------------------------------------- normalize_decision


def test_normalize_decision_recognises_common_aliases() -> None:
    assert normalize_decision("Accepted") == Decision.ACCEPTED
    assert normalize_decision("  accept ") == Decision.ACCEPTED
    assert normalize_decision("WAITLISTED") == Decision.WAITLIST
    assert normalize_decision("Reject") == Decision.REJECTED


def test_normalize_decision_returns_none_for_blank_or_unknown() -> None:
    assert normalize_decision("") is None
    assert normalize_decision("   ") is None
    assert normalize_decision("maybe") is None


# ------------------------------------------------------------- validate_score_row


def test_valid_accepted_row_has_no_issues() -> None:
    row = parse_score_row(_row(), row_number=1)
    issues, decision, division_placed = validate_score_row(row, KNOWN_DIVISIONS)
    assert issues == []
    assert decision == Decision.ACCEPTED
    assert division_placed == DivisionCode.CREATIVE


def test_blank_decision_is_rejected_never_defaulted() -> None:
    """SPEC.md §10.4: 'a blank must be a hard failure, never a default to
    rejected' — this is the single most load-bearing test in this module."""
    row = parse_score_row(_row(decision=""), row_number=1)
    issues, decision, _ = validate_score_row(row, KNOWN_DIVISIONS)
    assert decision is None
    assert any(
        i.reason_code == "BLANK_OR_UNKNOWN_DECISION" and i.outcome == "REJECTED" for i in issues
    )


def test_unrecognised_decision_text_is_rejected() -> None:
    row = parse_score_row(_row(decision="maybe next year"), row_number=1)
    issues, decision, _ = validate_score_row(row, KNOWN_DIVISIONS)
    assert decision is None
    assert any(i.reason_code == "BLANK_OR_UNKNOWN_DECISION" for i in issues)


def test_missing_applicant_id_is_rejected() -> None:
    row = parse_score_row(_row(applicant_id=""), row_number=1)
    issues, _, _ = validate_score_row(row, KNOWN_DIVISIONS)
    assert any(i.reason_code == "MISSING_APPLICANT_ID" for i in issues)


def test_accepted_without_division_placed_is_rejected() -> None:
    row = parse_score_row(_row(division_placed=""), row_number=1)
    issues, _, division_placed = validate_score_row(row, KNOWN_DIVISIONS)
    assert division_placed is None
    assert any(
        i.reason_code == "MISSING_DIVISION_PLACED" and i.outcome == "REJECTED" for i in issues
    )


def test_accepted_with_unknown_division_placed_is_rejected() -> None:
    row = parse_score_row(_row(division_placed="NOT_A_DIVISION"), row_number=1)
    issues, _, division_placed = validate_score_row(row, KNOWN_DIVISIONS)
    assert division_placed is None
    assert any(i.reason_code == "UNKNOWN_DIVISION_PLACED" for i in issues)


def test_rejected_with_division_placed_set_is_a_warning_not_a_rejection() -> None:
    row = parse_score_row(_row(decision="Reject", division_placed="CREATIVE"), row_number=1)
    issues, decision, division_placed = validate_score_row(row, KNOWN_DIVISIONS)
    assert decision == Decision.REJECTED
    assert division_placed is None
    assert any(
        i.reason_code == "DIVISION_PLACED_IGNORED" and i.outcome == "WARNING" for i in issues
    )
    assert not any(i.outcome == "REJECTED" for i in issues)


def test_waitlist_row_needs_no_division_placed() -> None:
    row = parse_score_row(_row(decision="Waitlist", division_placed=""), row_number=1)
    issues, decision, division_placed = validate_score_row(row, KNOWN_DIVISIONS)
    assert decision == Decision.WAITLIST
    assert division_placed is None
    assert issues == []


# ------------------------------------------------------------- run_ingest_scores


def test_run_ingest_scores_builds_clean_records_for_valid_rows() -> None:
    rows = [
        _row(applicant_id="A001", decision="Accepted", division_placed="CREATIVE"),
        _row(applicant_id="A002", decision="Waitlist", division_placed=""),
        _row(applicant_id="A003", decision="Reject", division_placed=""),
    ]
    result = run_ingest_scores(rows, KNOWN_DIVISIONS)

    assert {r.applicant_id: r.decision for r in result.records} == {
        "A001": Decision.ACCEPTED,
        "A002": Decision.WAITLIST,
        "A003": Decision.REJECTED,
    }
    assert result.report == []


def test_run_ingest_scores_drops_rows_with_blank_decision_from_records() -> None:
    rows = [_row(applicant_id="A001", decision="")]
    result = run_ingest_scores(rows, KNOWN_DIVISIONS)

    assert result.records == []
    assert any(r.outcome == "REJECTED" for r in result.report)


def test_run_ingest_scores_rejects_duplicate_applicant_id() -> None:
    """An ambiguous decision (two rows for one applicant) must not silently
    pick one — both are rejected until the sheet is fixed."""
    rows = [
        _row(applicant_id="A001", decision="Accepted"),
        _row(applicant_id="A001", decision="Reject", division_placed=""),
    ]
    result = run_ingest_scores(rows, KNOWN_DIVISIONS)

    assert result.records == []
    assert sum(1 for r in result.report if r.reason_code == "DUPLICATE_APPLICANT_ID") == 2


def test_write_score_outputs_produces_expected_csv_columns(tmp_path: Path) -> None:
    rows = [_row(applicant_id="A001", decision="Accepted", division_placed="CREATIVE")]
    result = run_ingest_scores(rows, KNOWN_DIVISIONS)

    clean_path = tmp_path / "decisions.clean.csv"
    report_path = tmp_path / "scores_validation_report.csv"
    write_score_outputs(result, clean_path, report_path)

    clean_df = pd.read_csv(clean_path, dtype=str, keep_default_na=False)
    assert list(clean_df.columns) == ["applicant_id", "decision", "division_placed"]
    assert clean_df.iloc[0]["decision"] == "ACCEPTED"

    report_df = pd.read_csv(report_path, dtype=str, keep_default_na=False)
    assert list(report_df.columns) == [
        "row_number",
        "applicant_id",
        "outcome",
        "reason_code",
        "message",
    ]


def test_parse_score_row_strips_whitespace() -> None:
    row = parse_score_row({"applicant_id": "  A001  ", "decision": " Accepted "}, row_number=1)
    assert row == RawScoreRow(
        row_number=1, applicant_id="A001", raw_decision="Accepted", raw_division_placed=""
    )
