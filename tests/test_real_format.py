"""End-to-end guard on the live IFF form's column layout.

`tests/fixtures/iff_real_format.csv` is a 10-row copy of the real Google
Form export: its header is the form's own, verbatim, down to the comma
inside "Current Location (e.g.: CBD, etc)" and the trailing "Column 1".
Names, emails, phone numbers and links are invented (CLAUDE.md invariant 7).

The point of this file is that a future refactor of ingest cannot silently
stop reading the real form. Every assertion here is about the mapping from
that exact header and those exact answer strings through to a solved
timetable, not about the scheduling logic (which the other suites cover).

The fixture's ten rows are:

  1  Amara Halim      Thursday, superseded by row 7 (same email)     COLLAPSED
  2  Benedikt Sanjaya Thursday
  3  Chloe Wijaya     Friday
  4  Daniyal Rahman   Friday
  5  Elina Kusumo     Thursday, both Media choices (same parent)
  6  Farhan Adhitama  Friday, both Creative choices (same parent)
  7  Amara Halim      Friday, the later submission from row 1's email
  8  Gita Prawira     Thursday, Liaison twice                        REJECTED
  9  Haris Nugroho    Thursday
 10  Indira Pratama   Friday
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

import pytest

from iff_scheduler.cli import _build_problem
from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.ingest.csv_source import CsvApplicantSource
from iff_scheduler.ingest.validate import IngestResult, run_ingest
from iff_scheduler.scheduling.base import USABLE_STATUSES, SolveResult
from iff_scheduler.scheduling.solver_cpsat import CpSatSolver
from iff_scheduler.settings import Settings, load_settings

FIXTURE = Path(__file__).parent / "fixtures" / "iff_real_format.csv"

# The header of the live form, in order. Ingest reads five of these columns
# and must ignore the rest without complaint.
REAL_HEADER = [
    "Timestamp",
    "Full Name",
    "University",
    "Major",
    "State of Degree",
    "Student ID",
    "Proof of Student Enrolment",
    "Phone Number (WhatsApp)",
    "Email Address",
    "Current Location (e.g.: CBD, etc)",
    "Social Media Accounts (Optional)",
    "First Preference",
    "Second Preference",
    "Preferred Interview Date",
    "Why do you want to join IFF?",
    "CV / Resume",
    "Google Drive Link",
    "Required Files",
    "Email Confirmation",
    "Column 1",
]

# Every option the live form offers for First / Second Preference.
LIVE_SUB_DIVISIONS = {
    "Logistics": DivisionCode.LOGISTICS,
    "Creative and Decor (Design and Decor)": DivisionCode.CREATIVE,
    "Creative and Decor (WebMaster)": DivisionCode.CREATIVE,
    "Media Marketing and Documentation (Documentation)": DivisionCode.MEDMARDOC,
    "Media Marketing and Documentation (Media Marketing)": DivisionCode.MEDMARDOC,
    "Finance and Booth": DivisionCode.FNB,
    "Program": DivisionCode.PROGRAM,
    "Liaison": DivisionCode.LIAISON,
}


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="module")
def result(settings: Settings) -> IngestResult:
    return run_ingest(
        source=CsvApplicantSource(path=FIXTURE),
        event=settings.event,
        divisions=settings.divisions,
        grid=build_slot_grid(settings.event),
    )


# ---- the fixture itself still looks like the real export ----


def test_fixture_header_is_the_live_forms_header_verbatim() -> None:
    with FIXTURE.open(newline="", encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert header == REAL_HEADER


def test_fixture_covers_every_case_the_real_sheet_produces() -> None:
    with FIXTURE.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 10
    days = [r["Preferred Interview Date"].split(",")[0] for r in rows]
    assert set(days) == {"Thursday", "Friday"}
    emails = [r["Email Address"] for r in rows]
    assert len(set(emails)) == 9, "one email must repeat, to exercise dedupe"


# ---- ingest reads it ----


def test_every_valid_registrant_is_accepted(result: IngestResult) -> None:
    """The only two rows that must not reach the clean list are the duplicate
    email and the row that picked the same sub-division twice. Anything else
    rejected here means a real applicant would silently lose their
    interviews (CLAUDE.md invariant 1)."""
    outcomes = [(r.row_number, r.outcome, r.reason_code) for r in result.report]
    assert outcomes == [
        (1, "COLLAPSED", "DUPLICATE_EMAIL"),
        (8, "REJECTED", "DUPLICATE_SUBDIVISION"),
    ]
    assert len(result.applicants) == 8


def test_the_columns_ingest_uses_land_in_the_right_fields(
    result: IngestResult,
) -> None:
    """The comma inside "Current Location (e.g.: CBD, etc)" and the trailing
    "Column 1" must not shift any field along."""
    elina = next(a for a in result.applicants if a.email == "elina.kusumo@example.com")
    assert elina.full_name == "Elina Kusumo"
    assert elina.phone == "0421000005"
    assert elina.student_id == "36020005"
    assert elina.sub_division_1 == "Media Marketing and Documentation (Media Marketing)"
    assert elina.sub_division_2 == "Media Marketing and Documentation (Documentation)"
    assert elina.notes is None


def test_timestamps_are_read_day_first_as_the_live_sheet_writes_them(
    result: IngestResult,
) -> None:
    benedikt = next(a for a in result.applicants if a.full_name == "Benedikt Sanjaya")
    assert benedikt.submitted_at == datetime(2025, 9, 12, 9, 12, 44)


@pytest.mark.parametrize(("name", "division"), sorted(LIVE_SUB_DIVISIONS.items()))
def test_every_live_form_option_maps_to_a_parent_division(
    settings: Settings, name: str, division: DivisionCode
) -> None:
    assert settings.divisions.canonical_sub_division_mapping[name.lower()] is division


def test_all_eight_live_options_appear_somewhere_in_the_clean_data(
    result: IngestResult,
) -> None:
    """A guard on the fixture rather than the code: if an option stops being
    exercised here, this file stops protecting it."""
    used = {a.sub_division_1 for a in result.applicants} | {
        a.sub_division_2 for a in result.applicants
    }
    assert used == set(LIVE_SUB_DIVISIONS)


# ---- the form's day answers become the configured event days ----


def test_thursday_and_friday_answers_open_the_matching_event_day(
    result: IngestResult, settings: Settings
) -> None:
    """The form says 2025 and the event is configured for 2026: only the
    weekday name is read (CLAUDE.md invariant 3)."""
    grid = build_slot_grid(settings.event)
    thursday, friday = date(2026, 9, 17), date(2026, 9, 18)
    slots_on = {day: {s.slot_id for s in grid.slots if s.date == day} for day in (thursday, friday)}

    benedikt = next(a for a in result.applicants if a.full_name == "Benedikt Sanjaya")
    chloe = next(a for a in result.applicants if a.full_name == "Chloe Wijaya")

    assert set(benedikt.availability_slots) == slots_on[thursday]
    assert set(chloe.availability_slots) == slots_on[friday]


def test_the_later_submission_wins_a_duplicate_email(result: IngestResult) -> None:
    amara = next(a for a in result.applicants if a.email == "amara.halim@example.com")
    assert amara.submitted_at == datetime(2025, 9, 13, 8, 30)
    assert (amara.sub_division_1, amara.sub_division_2) == ("Program", "Finance and Booth")


# ---- and the whole thing still solves ----


@pytest.fixture(scope="module")
def solved(result: IngestResult, settings: Settings) -> SolveResult:
    """Solve the fixture through exactly the path `iffsched solve` uses."""
    return CpSatSolver().solve(_build_problem(settings, result.applicants, []))


def test_the_real_format_solves_with_every_interview_placed(
    result: IngestResult, solved: SolveResult
) -> None:
    assert solved.status in USABLE_STATUSES
    assert len(solved.assignments) == 2 * len(result.applicants) == 16
    assert solved.clash_count == 0


def test_every_applicant_gets_exactly_two_interviews(
    result: IngestResult, solved: SolveResult
) -> None:
    """CLAUDE.md invariant 1, checked against the real column layout rather
    than a hand-built scenario."""
    by_applicant: dict[str, list[int]] = {}
    for a in solved.assignments:
        by_applicant.setdefault(a.applicant_id, []).append(a.choice_index)

    assert set(by_applicant) == {a.applicant_id for a in result.applicants}
    assert all(sorted(choices) == [1, 2] for choices in by_applicant.values())


def test_a_same_parent_pair_stays_two_separate_interviews(
    result: IngestResult, solved: SolveResult
) -> None:
    """Both Media choices, and both Creative choices, map to one parent
    division. Indexing by division rather than by choice would collapse each
    pair into a single interview (CLAUDE.md invariant 2)."""
    for name in ("Elina Kusumo", "Farhan Adhitama"):
        applicant = next(a for a in result.applicants if a.full_name == name)
        assert applicant.division_1 is applicant.division_2

        theirs = sorted(
            (a for a in solved.assignments if a.applicant_id == applicant.applicant_id),
            key=lambda a: a.choice_index,
        )
        assert [a.choice_index for a in theirs] == [1, 2]
        assert theirs[0].sub_division != theirs[1].sub_division
        assert theirs[0].slot_id != theirs[1].slot_id
