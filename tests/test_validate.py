"""Tests for ingest/validate.py — the rejection rules (FR-04, FR-06; SPEC.md §12)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.ingest.csv_source import CsvApplicantSource
from iff_scheduler.ingest.normalize import ParsedRow
from iff_scheduler.ingest.validate import (
    UNKNOWN_SUBMITTED_AT,
    run_ingest,
    validate_row,
    write_outputs,
)
from iff_scheduler.settings import load_settings

FIXTURE = Path(__file__).parent / "fixtures" / "applicants_raw.csv"


class _InMemorySource:
    """ApplicantSource over hand-written rows, for cases a fixture file would
    only obscure."""

    def __init__(self, rows: list[dict[str, str]]) -> None:
        self._rows = rows

    def read_raw(self) -> pd.DataFrame:
        return pd.DataFrame(self._rows, dtype=str)


def _valid_row(**overrides: object) -> ParsedRow:
    base: dict[str, object] = dict(
        row_number=1,
        email="valid@example.com",
        full_name="Valid Person",
        phone="+62-812",
        sub_division_1="Creative",
        sub_division_2="WebMaster",
        division_1=DivisionCode.CREATIVE,
        division_2=DivisionCode.CREATIVE,
        availability_slots=[
            "2026-09-17_1800",
            "2026-09-17_1820",
            "2026-09-17_1840",
            "2026-09-17_1900",
        ],
        submitted_at=datetime(2026, 8, 1, 9, 0),
        notes=None,
    )
    base.update(overrides)
    return ParsedRow(**base)  # type: ignore[arg-type]


def test_valid_row_has_no_issues() -> None:
    assert validate_row(_valid_row()) == []


def test_duplicate_subdivision_collapses_to_a_single_interview_not_a_rejection() -> None:
    """E-01b: the identical sub-division twice is read as a single-choice
    submission — one interview, not two, and not a rejection."""
    row = _valid_row(sub_division_1="Program", sub_division_2="Program")
    issues = validate_row(row)
    assert any(i.reason_code == "DUPLICATE_SUBDIVISION" and i.outcome == "WARNING" for i in issues)
    assert not any(i.outcome == "REJECTED" for i in issues)


def test_duplicate_subdivision_applicant_reaches_clean_list_with_one_interview() -> None:
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    raw = {
        "Timestamp": "15/08/2026 10:00:00",
        "Full Name": "Same Twice",
        "Email Address": "sametwice@example.com",
        "Phone Number (WhatsApp)": "+62-812-0002",
        "First Preference": "Program",
        "Second Preference": "Program",
        "Preferred Interview Date": "Thursday, 18 September 2025",
    }
    result = run_ingest(_InMemorySource([raw]), settings.event, settings.divisions, grid)

    assert [a.email for a in result.applicants] == ["sametwice@example.com"]
    applicant = result.applicants[0]
    assert applicant.single_choice is True
    assert applicant.division_2 is None
    assert applicant.sub_division_2 == ""
    assert applicant.sub_division_1 == "Program"
    assert not any(r.outcome == "REJECTED" for r in result.report)
    assert any(
        r.reason_code == "DUPLICATE_SUBDIVISION" and r.outcome == "WARNING" for r in result.report
    )


def test_no_availability_is_a_warning_not_a_rejection() -> None:
    """E-02 relaxed: a blank availability answer is assumed to mean "free for
    the whole event" and flagged, not dropped."""
    row = _valid_row(availability_slots=[])
    issues = validate_row(row)
    assert any(i.reason_code == "NO_AVAILABILITY" and i.outcome == "WARNING" for i in issues)
    assert not any(i.outcome == "REJECTED" for i in issues)


def test_no_availability_applicant_reaches_clean_list_with_full_availability() -> None:
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    raw = {
        "Timestamp": "15/08/2026 10:00:00",
        "Full Name": "No Availability",
        "Email Address": "noavail@example.com",
        "Phone Number (WhatsApp)": "+62-812-0000",
        "First Preference": "Logistics",
        "Second Preference": "Program",
        "Preferred Interview Date": "",
    }
    result = run_ingest(_InMemorySource([raw]), settings.event, settings.divisions, grid)

    assert [a.email for a in result.applicants] == ["noavail@example.com"]
    assert result.applicants[0].availability_slots == [s.slot_id for s in grid.slots]
    assert any(r.reason_code == "NO_AVAILABILITY" and r.outcome == "WARNING" for r in result.report)


def test_single_choice_row_is_accepted_with_one_interview() -> None:
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    raw = {
        "Timestamp": "15/08/2026 10:00:00",
        "Full Name": "Single Choice",
        "Email Address": "single@example.com",
        "Phone Number (WhatsApp)": "+62-812-0001",
        "First Preference": "Logistics",
        "Second Preference": "",
        "Preferred Interview Date": "Thursday, 18 September 2025",
    }
    result = run_ingest(_InMemorySource([raw]), settings.event, settings.divisions, grid)

    assert [a.email for a in result.applicants] == ["single@example.com"]
    applicant = result.applicants[0]
    assert applicant.single_choice is True
    assert applicant.division_2 is None
    assert applicant.sub_division_2 == ""
    assert not any(r.outcome == "REJECTED" for r in result.report)


def test_both_choices_blank_is_still_rejected() -> None:
    row = _valid_row(sub_division_1="", sub_division_2="", division_1=None, division_2=None)
    issues = validate_row(row)
    assert any(i.reason_code == "MISSING_SUBDIVISION" and i.outcome == "REJECTED" for i in issues)


def test_same_parent_different_subdivisions_is_not_a_duplicate() -> None:
    row = _valid_row(
        sub_division_1="Media Marketing",
        sub_division_2="Media Documentation",
        division_1=DivisionCode.MEDMARDOC,
        division_2=DivisionCode.MEDMARDOC,
    )
    issues = validate_row(row)
    assert not any(i.reason_code == "DUPLICATE_SUBDIVISION" for i in issues)


def test_sparse_availability_is_a_warning_not_a_rejection() -> None:
    row = _valid_row(availability_slots=["2026-09-17_1800"])
    issues = validate_row(row)
    assert any(i.reason_code == "SPARSE_AVAILABILITY" and i.outcome == "WARNING" for i in issues)
    assert not any(i.outcome == "REJECTED" for i in issues)


def test_unknown_subdivision_is_rejected() -> None:
    row = _valid_row(sub_division_1="Not A Real Division", division_1=None)
    issues = validate_row(row)
    assert any(i.reason_code == "UNKNOWN_SUBDIVISION" and i.outcome == "REJECTED" for i in issues)


def test_missing_full_name_is_rejected() -> None:
    issues = validate_row(_valid_row(full_name=""))
    assert any(i.reason_code == "MISSING_FULL_NAME" for i in issues)


def test_missing_email_is_rejected() -> None:
    issues = validate_row(_valid_row(email=""))
    assert any(i.reason_code == "MISSING_EMAIL" for i in issues)


def test_invalid_email_is_rejected() -> None:
    issues = validate_row(_valid_row(email="not-an-email"))
    assert any(i.reason_code == "INVALID_EMAIL" for i in issues)


def test_missing_timestamp_warns_but_does_not_reject() -> None:
    """The timestamp only orders duplicate submissions, so an unreadable one
    must not cost a real registrant their interviews (CLAUDE.md invariant 1)."""
    issues = validate_row(_valid_row(submitted_at=None))
    assert [i.reason_code for i in issues if i.outcome == "REJECTED"] == []
    assert any(i.reason_code == "MISSING_TIMESTAMP" and i.outcome == "WARNING" for i in issues)


def test_applicant_with_unreadable_timestamp_still_reaches_the_clean_list() -> None:
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    raw = {
        "Timestamp": "",
        "Full Name": "Undated Applicant",
        "Email Address": "undated@example.com",
        "Phone Number (WhatsApp)": "+62-812-9999",
        "First Preference": "Logistics",
        "Second Preference": "Program",
        "Preferred Interview Date": "Thursday, 18 September 2025",
    }
    result = run_ingest(_InMemorySource([raw]), settings.event, settings.divisions, grid)

    assert [a.email for a in result.applicants] == ["undated@example.com"]
    assert result.applicants[0].submitted_at == UNKNOWN_SUBMITTED_AT


# ---- end-to-end against the fixture CSV ----


def test_run_ingest_against_fixture() -> None:
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    source = CsvApplicantSource(path=FIXTURE)

    result = run_ingest(source, settings.event, settings.divisions, grid)

    assert {a.email for a in result.applicants} == {
        "ayu@example.com",
        "bagas@example.com",
        # Citra picked "Program" twice — no longer a rejection, collapsed to a
        # single-choice applicant with one interview instead (E-01b relaxed).
        "citra@example.com",
        # Dimas left the availability question blank — no longer a rejection,
        # assumed free for the whole event instead (E-02 relaxed).
        "dimas@example.com",
        "eka@example.com",
        "fajar@example.com",
        "hendra@example.com",
    }
    assert len(result.applicants) == 7

    citra = next(a for a in result.applicants if a.email == "citra@example.com")
    assert citra.single_choice is True
    assert citra.division_2 is None

    rejected = {(r.email, r.reason_code) for r in result.report if r.outcome == "REJECTED"}
    assert rejected == {
        ("gita@example.com", "UNKNOWN_SUBDIVISION"),
        ("indah@example.com", "MISSING_FULL_NAME"),
    }

    collapsed = [r for r in result.report if r.outcome == "COLLAPSED"]
    assert len(collapsed) == 1
    assert collapsed[0].email == "eka@example.com"
    assert collapsed[0].reason_code == "DUPLICATE_EMAIL"

    warnings = {(r.email, r.reason_code) for r in result.report if r.outcome == "WARNING"}
    assert warnings == {
        ("citra@example.com", "DUPLICATE_SUBDIVISION"),
        ("dimas@example.com", "NO_AVAILABILITY"),
    }


def test_same_parent_pair_yields_two_interviews_not_one() -> None:
    """E-01: both choices must survive even though they share a parent division."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    result = run_ingest(CsvApplicantSource(path=FIXTURE), settings.event, settings.divisions, grid)

    ayu = next(a for a in result.applicants if a.email == "ayu@example.com")
    assert ayu.division_1 == DivisionCode.MEDMARDOC
    assert ayu.division_2 == DivisionCode.MEDMARDOC
    assert ayu.sub_division_1 != ayu.sub_division_2


def test_eka_kept_row_is_the_later_submission() -> None:
    """E-04: the surviving row must be the one with the later timestamp."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    result = run_ingest(CsvApplicantSource(path=FIXTURE), settings.event, settings.divisions, grid)

    eka = next(a for a in result.applicants if a.email == "eka@example.com")
    assert eka.sub_division_1 == "WebMaster"
    assert eka.submitted_at == datetime(2026, 8, 10, 9, 0)


def test_write_outputs_produces_expected_csv_columns(tmp_path: Path) -> None:
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    result = run_ingest(CsvApplicantSource(path=FIXTURE), settings.event, settings.divisions, grid)

    clean_path = tmp_path / "applicants.clean.csv"
    report_path = tmp_path / "validation_report.csv"
    write_outputs(result, clean_path, report_path)

    clean_df = pd.read_csv(clean_path, dtype=str, keep_default_na=False)
    assert list(clean_df.columns) == [
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
    assert len(clean_df) == 7
    assert clean_df["applicant_id"].is_unique

    report_df = pd.read_csv(report_path, dtype=str, keep_default_na=False)
    assert list(report_df.columns) == [
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
    # 3 rejections + 1 collapsed duplicate + 1 assumed-full-availability warning.
    assert len(report_df) == 5
