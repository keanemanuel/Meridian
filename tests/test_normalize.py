"""Tests for ingest/normalize.py (FR-02, FR-03, FR-05; SPEC.md §12 E-01, E-04, E-05)."""

from __future__ import annotations

from datetime import date, datetime, time

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.ingest.normalize import (
    ParsedRow,
    blocks_to_slot_ids,
    dedupe_by_email,
    fold_header,
    map_sub_division,
    merge_windows,
    parse_availability_cell,
    parse_preferred_dates,
    parse_row,
    parse_timestamp,
    resolve_columns,
)
from iff_scheduler.settings import DayConfig, DivisionEntry, DivisionsConfig, EventConfig

DIVISIONS = DivisionsConfig(
    divisions=[
        DivisionEntry(code=DivisionCode.MEDMARDOC, display="Media Marketing & Documentation"),
        DivisionEntry(code=DivisionCode.CREATIVE, display="Creative"),
    ],
    sub_division_mapping={
        "Media Marketing": DivisionCode.MEDMARDOC,
        "Media Documentation": DivisionCode.MEDMARDOC,
        "Creative": DivisionCode.CREATIVE,
        "WebMaster": DivisionCode.CREATIVE,
    },
)


def _event(matching: str = "strict") -> EventConfig:
    return EventConfig(
        event_name="Test",
        timezone="Asia/Jakarta",
        interview_duration_minutes=20,
        availability_matching=matching,
        days=[DayConfig(date=date(2026, 9, 17), label="Thu", start=time(18, 0), end=time(18, 40))],
    )


def _two_day_event() -> EventConfig:
    return EventConfig(
        event_name="Test",
        timezone="Asia/Jakarta",
        interview_duration_minutes=20,
        days=[
            DayConfig(date=date(2026, 9, 17), label="Thu", start=time(18, 0), end=time(19, 0)),
            DayConfig(date=date(2026, 9, 18), label="Fri", start=time(17, 0), end=time(18, 0)),
        ],
    )


# ---- parse_preferred_dates (the live IFF form's availability capture) ----


def test_preferred_dates_matches_weekday_name_not_literal_date() -> None:
    # The form's calendar year (2025) differs from the event year (2026);
    # matching is by weekday, and the whole day's window comes back.
    out = parse_preferred_dates("Thursday, 18 September 2025", _two_day_event())
    assert out == {date(2026, 9, 17): [(time(18, 0), time(19, 0))]}


def test_preferred_dates_handles_multiple_days_in_one_cell() -> None:
    out = parse_preferred_dates(
        "Thursday, 18 September 2025, Friday, 19 September 2025", _two_day_event()
    )
    assert set(out) == {date(2026, 9, 17), date(2026, 9, 18)}


def test_preferred_dates_blank_or_unmatched_is_empty() -> None:
    assert parse_preferred_dates("", _two_day_event()) == {}
    assert parse_preferred_dates("Sometime next week", _two_day_event()) == {}


def test_parse_row_full_day_availability_covers_every_slot_that_day() -> None:
    event = _two_day_event()
    grid = build_slot_grid(event)
    raw = {
        "Timestamp": "8/20/2026 15:45:00",
        "Email Address": "ayu@example.com",
        "Full Name": "Ayu",
        "First Preference": "Creative",
        "Second Preference": "WebMaster",
        "Preferred Interview Date": "Friday, 19 September 2025",
    }
    row = parse_row(raw, 1, event, DIVISIONS, grid)
    assert row.submitted_at == datetime(2026, 8, 20, 15, 45)
    assert row.availability_slots == ["2026-09-18_1700", "2026-09-18_1720", "2026-09-18_1740"]


# ---- parse_availability_cell ----


def test_parse_availability_cell_splits_and_parses_blocks() -> None:
    windows = parse_availability_cell("18:00-18:30, 19:00 - 19:30")
    assert windows == [(time(18, 0), time(18, 30)), (time(19, 0), time(19, 30))]


def test_parse_availability_cell_blank_is_empty() -> None:
    assert parse_availability_cell("") == []
    assert parse_availability_cell("   ") == []


# ---- merge_windows ----


def test_merge_windows_joins_adjacent_blocks() -> None:
    merged = merge_windows([(time(18, 0), time(18, 30)), (time(18, 30), time(19, 0))])
    assert merged == [(time(18, 0), time(19, 0))]


def test_merge_windows_keeps_disjoint_blocks_separate() -> None:
    merged = merge_windows([(time(18, 0), time(18, 30)), (time(20, 0), time(20, 30))])
    assert merged == [(time(18, 0), time(18, 30)), (time(20, 0), time(20, 30))]


# ---- blocks_to_slot_ids: E-05 (block size mismatch) ----


def test_strict_matching_requires_full_containment() -> None:
    # 20-minute grid inside an 18:00-18:40 day; one 30-minute tick 18:00-18:30
    # leaves the second grid slot (18:20-18:40) straddling the boundary.
    grid = build_slot_grid(_event("strict"))
    slot_ids = blocks_to_slot_ids(
        {date(2026, 9, 17): [(time(18, 0), time(18, 30))]}, grid, "strict"
    )
    assert slot_ids == ["2026-09-17_1800"]


def test_lenient_matching_counts_majority_overlap() -> None:
    grid = build_slot_grid(_event("lenient"))
    slot_ids = blocks_to_slot_ids(
        {date(2026, 9, 17): [(time(18, 0), time(18, 30))]}, grid, "lenient"
    )
    # 18:20-18:40 overlaps the tick by 10 of its 20 minutes -> exactly 50%, counts.
    assert slot_ids == ["2026-09-17_1800", "2026-09-17_1820"]


def test_adjacent_ticked_blocks_cover_a_slot_that_spans_their_boundary() -> None:
    grid = build_slot_grid(_event("strict"))
    slot_ids = blocks_to_slot_ids(
        {date(2026, 9, 17): [(time(18, 0), time(18, 30)), (time(18, 30), time(18, 40))]},
        grid,
        "strict",
    )
    assert slot_ids == ["2026-09-17_1800", "2026-09-17_1820"]


# ---- map_sub_division ----


def test_map_sub_division_known_and_unknown() -> None:
    assert map_sub_division("Creative", DIVISIONS.sub_division_mapping) == DivisionCode.CREATIVE
    assert map_sub_division("Marketing Typo", DIVISIONS.sub_division_mapping) is None


# ---- parse_timestamp ----


def test_parse_timestamp_accepts_iso_and_rejects_garbage() -> None:
    assert parse_timestamp("2026-08-20T15:45:00") == datetime(2026, 8, 20, 15, 45, 0)
    assert parse_timestamp("not a date") is None
    assert parse_timestamp("") is None


# ---- parse_row (E-01: same-parent pair is valid at the parsing stage) ----


def test_parse_row_resolves_same_parent_pair() -> None:
    grid = build_slot_grid(_event())
    raw = {
        "Timestamp": "2026-08-20T15:45:00",
        "Email Address": "  Ayu@Example.com ",
        "Full Name": "Ayu Prameswari",
        "Phone Number (WhatsApp)": "+62 812",
        "Student ID": "IFF-0001",
        "First Preference": "Media Marketing",
        "Second Preference": "Media Documentation",
        "Preferred Interview Date": "Thursday, 18 September 2025",
    }
    row = parse_row(raw, 1, _event(), DIVISIONS, grid)
    assert row.email == "ayu@example.com"
    assert row.student_id == "IFF-0001"
    assert row.division_1 == DivisionCode.MEDMARDOC
    assert row.division_2 == DivisionCode.MEDMARDOC
    assert row.sub_division_1 != row.sub_division_2
    # A picked day = every slot that day (the test event day is 18:00-18:40).
    assert row.availability_slots == ["2026-09-17_1800", "2026-09-17_1820"]


def test_parse_row_leaves_unknown_subdivision_as_none_rather_than_guessing() -> None:
    grid = build_slot_grid(_event())
    raw = {
        "Timestamp": "2026-08-20T15:45:00",
        "Email Address": "gita@example.com",
        "Full Name": "Gita Ayu Lestari",
        "First Preference": "Media Markting",
        "Second Preference": "Creative",
        "Preferred Interview Date": "Thursday, 18 September 2025",
    }
    row = parse_row(raw, 1, _event(), DIVISIONS, grid)
    assert row.division_1 is None
    assert row.division_2 == DivisionCode.CREATIVE


# ---- dedupe_by_email (E-04) ----


def _row(row_number: int, email: str, submitted_at: datetime | None) -> ParsedRow:
    return ParsedRow(
        row_number=row_number,
        email=email,
        full_name=f"Person {row_number}",
        phone="",
        sub_division_1="Creative",
        sub_division_2="WebMaster",
        division_1=DivisionCode.CREATIVE,
        division_2=DivisionCode.CREATIVE,
        availability_slots=["2026-09-17_1800"],
        submitted_at=submitted_at,
        notes=None,
    )


def test_dedupe_by_email_keeps_latest_and_reports_collapse() -> None:
    old = _row(1, "eka@example.com", datetime(2026, 8, 1, 9, 0))
    new = _row(2, "eka@example.com", datetime(2026, 8, 10, 9, 0))
    kept, collapsed = dedupe_by_email([old, new])
    assert kept == [new]
    assert collapsed == [old]


def test_dedupe_by_email_leaves_unique_emails_alone() -> None:
    a = _row(1, "a@example.com", datetime(2026, 8, 1, 9, 0))
    b = _row(2, "b@example.com", datetime(2026, 8, 1, 9, 0))
    kept, collapsed = dedupe_by_email([a, b])
    assert kept == [a, b]
    assert collapsed == []


def test_dedupe_by_email_never_groups_blank_emails() -> None:
    a = _row(1, "", datetime(2026, 8, 1, 9, 0))
    b = _row(2, "", datetime(2026, 8, 1, 9, 0))
    kept, collapsed = dedupe_by_email([a, b])
    assert kept == [a, b]
    assert collapsed == []


def test_dedupe_by_email_missing_timestamp_loses_tiebreak_to_row_order() -> None:
    no_ts = _row(1, "x@example.com", None)
    with_ts = _row(2, "x@example.com", datetime(2026, 8, 1, 9, 0))
    kept, collapsed = dedupe_by_email([no_ts, with_ts])
    assert kept == [with_ts]
    assert collapsed == [no_ts]


# ---- real IFF form: header aliasing and sub-division mapping ----


def test_fold_header_drops_trailing_parenthetical_and_case() -> None:
    assert fold_header("Phone Number (WhatsApp)") == "phone number"
    assert fold_header("Current Location (e.g.: CBD, etc)") == "current location"
    assert fold_header("  Email   Address ") == "email address"


def test_resolve_columns_ignores_the_form_columns_ingest_does_not_use() -> None:
    """Extra questions and trailing filler like "Column 1" must not affect the
    fields ingest rules on — adding a question to the form cannot empty the
    schedule."""
    raw = {
        "Timestamp": "12/09/2025 14:20:59",
        "Full Name": "Jane Doe",
        "University": "Monash",
        "Major": "Business",
        "State of Degree": "Year 1 Semester 2",
        "Student ID": "36021679",
        "Proof of Student Enrolment": "https://drive.example/proof",
        "Phone Number (WhatsApp)": "0421824484",
        "Email Address": "jane@example.com",
        "Current Location (e.g.: CBD, etc)": "18 Leicester Street",
        "Social Media Accounts (Optional)": "@jane",
        "First Preference": "Logistics",
        "Second Preference": "Creative and Decor (Design and Decor)",
        "Preferred Interview Date": "Thursday, 18 September 2025, 18.00 - 21.30 AEST",
        "Why do you want to join IFF?": "essay",
        "CV / Resume": "https://drive.example/cv",
        "Google Drive Link": "",
        "Required Files": "files",
        "Email Confirmation": "TRUE",
        "Column 1": "",
    }
    cell = resolve_columns(raw)
    assert cell["Email Address"] == "jane@example.com"
    assert cell["Phone Number (WhatsApp)"] == "0421824484"
    assert cell["First Preference"] == "Logistics"
    assert cell["Accessibility / scheduling notes"] == ""


def test_map_sub_division_folds_case_and_whitespace_but_not_meaning() -> None:
    mapping = {"Finance and Booth": DivisionCode.FNB}
    assert map_sub_division("  finance and  booth ", mapping) is DivisionCode.FNB
    assert map_sub_division("Finance", mapping) is None
    assert map_sub_division("", mapping) is None


def test_live_form_sub_divisions_all_map_to_a_parent_division() -> None:
    """Every option the live form offers must resolve, or valid registrants are
    rejected as UNKNOWN_SUBDIVISION."""
    from iff_scheduler.settings import load_settings

    mapping = load_settings().divisions.canonical_sub_division_mapping
    expected = {
        "Logistics": DivisionCode.LOGISTICS,
        "Creative and Decor (Design and Decor)": DivisionCode.CREATIVE,
        "Creative and Decor (WebMaster)": DivisionCode.CREATIVE,
        "Media Marketing and Documentation (Documentation)": DivisionCode.MEDMARDOC,
        "Media Marketing and Documentation (Media Marketing)": DivisionCode.MEDMARDOC,
        "Finance and Booth": DivisionCode.FNB,
        "Program": DivisionCode.PROGRAM,
        "Liaison": DivisionCode.LIAISON,
    }
    assert {name: map_sub_division(name, mapping) for name in expected} == expected


def test_parse_timestamp_reads_the_live_sheets_day_first_locale() -> None:
    assert parse_timestamp("12/09/2025 14:20:59") == datetime(2025, 9, 12, 14, 20, 59)
    # Unambiguous month-first values still parse: 15 is not a month.
    assert parse_timestamp("8/15/2026 10:00:00") == datetime(2026, 8, 15, 10, 0)


def test_preferred_date_ignores_the_forms_calendar_year_and_time_text() -> None:
    """The form says 2025 while the event is configured for 2026; only the
    weekday name is read (CLAUDE.md invariant 3 — nothing is guessed beyond it)."""
    event = _two_day_event()
    thursday = parse_preferred_dates("Thursday, 18 September 2025, 18.00 - 21.30 AEST", event)
    friday = parse_preferred_dates("Friday, 19 September 2025, 18.00 - 21.30 AEST", event)
    assert list(thursday) == [date(2026, 9, 17)]
    assert list(friday) == [date(2026, 9, 18)]
