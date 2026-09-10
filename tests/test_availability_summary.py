"""Tests for the declared-availability summary shown on the Applicants view
(FR-51). The live IFF form collects whole days, so a fully-available day
collapses to just its label; explicit time blocks show merged ranges."""

from __future__ import annotations

from datetime import date, time

from iff_scheduler.domain.availability import summarise_availability
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.settings import DayConfig, EventConfig

THU = date(2026, 9, 17)
FRI = date(2026, 9, 18)


def _slots() -> list:
    event = EventConfig(
        event_name="Test Event",
        timezone="Asia/Jakarta",
        interview_duration_minutes=60,
        days=[
            DayConfig(date=THU, label="Thu", start=time(18, 0), end=time(21, 0), breaks=[]),
            DayConfig(date=FRI, label="Fri", start=time(18, 0), end=time(21, 0), breaks=[]),
        ],
    )
    return build_slot_grid(event).slots


def test_empty_availability_renders_as_blank() -> None:
    assert summarise_availability([], _slots()) == ""


def test_a_fully_available_day_collapses_to_its_label() -> None:
    slots = _slots()
    thu = [s.slot_id for s in slots if s.date == THU]
    assert summarise_availability(thu, slots) == "Thu 17 Sep"


def test_both_full_days_are_joined() -> None:
    slots = _slots()
    every = [s.slot_id for s in slots]
    assert summarise_availability(every, slots) == "Thu 17 Sep; Fri 18 Sep"


def test_a_partial_day_shows_merged_time_ranges() -> None:
    slots = _slots()
    # First two contiguous Thursday slots only: 18:00-20:00.
    thu = sorted((s for s in slots if s.date == THU), key=lambda s: s.start_time)
    picked = [thu[0].slot_id, thu[1].slot_id]
    assert summarise_availability(picked, slots) == "Thu 17 Sep (18:00–20:00)"


def test_non_contiguous_blocks_stay_separate_ranges() -> None:
    slots = _slots()
    thu = sorted((s for s in slots if s.date == THU), key=lambda s: s.start_time)
    picked = [thu[0].slot_id, thu[2].slot_id]  # 18:00-19:00 and 20:00-21:00
    assert summarise_availability(picked, slots) == "Thu 17 Sep (18:00–19:00, 20:00–21:00)"


def test_slot_ids_not_on_the_grid_are_ignored() -> None:
    slots = _slots()
    assert summarise_availability(["9999-01-01_0000"], slots) == ""


def test_time_drifted_slot_ids_recover_to_whole_day_labels() -> None:
    """Regression: shortening/re-timing the event window after ingest shifts
    every stored slot id's HHMM suffix, so none match the grid by value. The
    date the applicant ticked survives, so the column shows the day label
    instead of going blank (FR-51)."""
    slots = _slots()  # Thu/Fri 18:00–21:00
    # What ingest stored against an earlier grid that started at 18:30 —
    # no id here is on the current grid.
    stored = ["2026-09-17_1830", "2026-09-17_1930", "2026-09-18_1830"]
    assert all(sid not in {s.slot_id for s in slots} for sid in stored)
    assert summarise_availability(stored, slots) == "Thu 17 Sep; Fri 18 Sep"


def test_exact_matches_still_win_over_whole_day_recovery() -> None:
    """A day with some ids still on the grid keeps its precise ranges; only the
    days that drifted entirely fall back to a bare label."""
    slots = _slots()
    thu = sorted((s for s in slots if s.date == THU), key=lambda s: s.start_time)
    picked = [thu[0].slot_id, thu[1].slot_id, "2026-09-18_1830"]  # Fri id drifted
    assert summarise_availability(picked, slots) == "Thu 17 Sep (18:00–20:00); Fri 18 Sep"
