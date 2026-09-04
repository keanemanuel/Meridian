"""Tests for slot grid generation (FR-10..FR-16, M0 definition of done)."""

from __future__ import annotations

from datetime import date, time

from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.settings import BreakWindow, DayConfig, EventConfig, load_settings

THU = date(2026, 9, 17)
FRI = date(2026, 9, 18)


def _event(duration: int, breaks: list[BreakWindow] | None = None) -> EventConfig:
    return EventConfig(
        event_name="Test Event",
        timezone="Asia/Jakarta",
        interview_duration_minutes=duration,
        days=[
            DayConfig(
                date=THU,
                label="Thu",
                start=time(18, 0),
                end=time(22, 0),
                breaks=breaks or [],
            ),
            DayConfig(
                date=FRI,
                label="Fri",
                start=time(18, 0),
                end=time(22, 0),
                breaks=[],
            ),
        ],
    )


def test_20_minute_grid_has_24_slots_across_two_days() -> None:
    grid = build_slot_grid(_event(20))
    assert len(grid.slots) == 24
    assert grid.warnings == []


def test_20_minute_grid_slot_boundaries() -> None:
    grid = build_slot_grid(_event(20))
    day_one = [s for s in grid.slots if s.date == THU]
    assert len(day_one) == 12
    assert day_one[0].start_time == time(18, 0)
    assert day_one[0].end_time == time(18, 20)
    assert day_one[1].start_time == time(18, 20)
    assert day_one[-1].end_time == time(22, 0)


def test_changing_duration_20_to_30_regenerates_grid() -> None:
    """FR-11: 20 -> 18:00-18:20, 18:20-18:40 ...; 30 -> 18:00-18:30, 18:30-19:00 ..."""
    grid_20 = build_slot_grid(_event(20))
    grid_30 = build_slot_grid(_event(30))

    assert len(grid_20.slots) == 24
    assert len(grid_30.slots) == 16

    day_one_30 = [s for s in grid_30.slots if s.date == THU]
    assert len(day_one_30) == 8
    assert day_one_30[0].start_time == time(18, 0)
    assert day_one_30[0].end_time == time(18, 30)
    assert day_one_30[1].start_time == time(18, 30)
    assert day_one_30[1].end_time == time(19, 0)
    assert day_one_30[-1].end_time == time(22, 0)


def test_slot_index_is_sequential_and_unique_across_days() -> None:
    grid = build_slot_grid(_event(20))
    assert [s.slot_index for s in grid.slots] == list(range(24))


def test_slot_ids_are_unique() -> None:
    grid = build_slot_grid(_event(20))
    ids = [s.slot_id for s in grid.slots]
    assert len(ids) == len(set(ids))


def test_break_window_removes_overlapping_slots() -> None:
    """FR-14: no slots are generated inside a configured break window."""
    breaks = [BreakWindow(start=time(19, 40), end=time(20, 0))]
    grid = build_slot_grid(_event(20, breaks=breaks))
    day_one = [s for s in grid.slots if s.date == THU]

    assert len(day_one) == 11
    assert time(19, 40) not in [s.start_time for s in day_one]
    assert not grid.warnings


def test_trailing_partial_slot_is_discarded_and_reported() -> None:
    """FR-15: 240 minutes / 25-minute interviews leaves a 15-minute remainder."""
    grid = build_slot_grid(_event(25))
    day_one = [s for s in grid.slots if s.date == THU]

    assert len(day_one) == 9
    assert day_one[-1].end_time == time(21, 45)
    assert any("trailing partial slot" in w and THU.isoformat() in w for w in grid.warnings)


def test_grid_is_deterministic() -> None:
    """FR-35: same config in, same grid out."""
    event = _event(20)
    assert build_slot_grid(event).slots == build_slot_grid(event).slots


def test_grid_regenerates_from_committed_event_config() -> None:
    """M0 definition of done: the committed config/event.yaml regenerates correctly,
    and bumping interview_duration_minutes 20 -> 30 regenerates the grid with no
    code change."""
    settings = load_settings()
    assert settings.event.interview_duration_minutes == 20

    grid = build_slot_grid(settings.event)
    assert len(grid.slots) == 24

    event_30 = settings.event.model_copy(update={"interview_duration_minutes": 30})
    grid_30 = build_slot_grid(event_30)
    assert len(grid_30.slots) == 16
