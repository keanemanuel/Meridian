"""Tests for ingest/normalize.py (FR-02, FR-03, FR-05; SPEC.md §12 E-01, E-04, E-05)."""

from __future__ import annotations

from datetime import date, datetime, time

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.ingest.normalize import (
    ParsedRow,
    blocks_to_slot_ids,
    dedupe_by_email,
    map_sub_division,
    merge_windows,
    parse_availability_cell,
    parse_row,
    parse_timestamp,
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
        "Email address": "  Ayu@Example.com ",
        "Full name": "Ayu Prameswari",
        "Phone / WhatsApp": "+62 812",
        "First-choice sub-division": "Media Marketing",
        "Second-choice sub-division": "Media Documentation",
        "Availability — Thu": "18:00-18:30",
        "Accessibility / scheduling notes": "",
    }
    row = parse_row(raw, 1, _event(), DIVISIONS, grid)
    assert row.email == "ayu@example.com"
    assert row.division_1 == DivisionCode.MEDMARDOC
    assert row.division_2 == DivisionCode.MEDMARDOC
    assert row.sub_division_1 != row.sub_division_2
    assert row.availability_slots == ["2026-09-17_1800"]


def test_parse_row_leaves_unknown_subdivision_as_none_rather_than_guessing() -> None:
    grid = build_slot_grid(_event())
    raw = {
        "Timestamp": "2026-08-20T15:45:00",
        "Email address": "gita@example.com",
        "Full name": "Gita Ayu Lestari",
        "First-choice sub-division": "Media Markting",
        "Second-choice sub-division": "Creative",
        "Availability — Thu": "18:00-18:30",
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
