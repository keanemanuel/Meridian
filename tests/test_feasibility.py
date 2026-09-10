"""Tests for scheduling/feasibility.py — the Capacity Advisor (SPEC.md §5.5,
§1.2 Finding A)."""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, time

import pytest
from pydantic import ValidationError

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.domain.models import Applicant
from iff_scheduler.scheduling.feasibility import (
    REBALANCE_THRESHOLD,
    autoscale_panels,
    balance_panel_rooms,
    compute_capacity_advisor,
    consolidate_panels,
    is_feasible,
    rebalance_panels,
)
from iff_scheduler.settings import (
    ROOM_CONCURRENCY_CEILING,
    ActiveWindow,
    DayConfig,
    EventConfig,
    PanelEntry,
    PanelsConfig,
    RoomsConfig,
    load_settings,
)

DAY = date(2026, 9, 17)


def _event() -> EventConfig:
    # 3 slots: 18:00-18:20, 18:20-18:40, 18:40-19:00
    return EventConfig(
        event_name="Test",
        timezone="Asia/Jakarta",
        interview_duration_minutes=20,
        days=[DayConfig(date=DAY, label="Thu", start=time(18, 0), end=time(19, 0))],
    )


def _applicant(
    applicant_id: str, division_1: DivisionCode, division_2: DivisionCode, slot_ids: list[str]
) -> Applicant:
    return Applicant(
        applicant_id=applicant_id,
        full_name=applicant_id,
        email=f"{applicant_id.lower()}@example.com",
        phone="",
        sub_division_1="X",
        sub_division_2="Y",
        division_1=division_1,
        division_2=division_2,
        availability_slots=slot_ids,
        submitted_at=datetime(2026, 8, 1, 9, 0),
        notes=None,
    )


def test_division_with_ample_panels_is_ok() -> None:
    grid = build_slot_grid(_event())
    all_slots = [s.slot_id for s in grid.slots]
    applicants = [
        _applicant("A1", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, all_slots),
        _applicant("A2", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, all_slots),
    ]
    panels = PanelsConfig(
        panels=[PanelEntry(id="CREATIVE-A", division=DivisionCode.CREATIVE, room="R1")]
    )

    rows = compute_capacity_advisor(applicants, panels, grid, target_utilisation=0.83)
    creative = next(r for r in rows if r.division == DivisionCode.CREATIVE)

    assert creative.demand == 2
    assert creative.panels_configured == 1
    assert creative.raw_supply == 3  # 1 panel x 3 slots
    assert creative.effective_supply == 3
    assert creative.recommended_panels == 1  # ceil(2 / (3 * 0.83)) = 1
    assert creative.verdict == "OK"


def test_division_below_recommended_panels_is_tight_but_not_infeasible() -> None:
    grid = build_slot_grid(_event())
    all_slots = [s.slot_id for s in grid.slots]
    applicants = [
        _applicant(f"A{i}", DivisionCode.LOGISTICS, DivisionCode.FNB, all_slots) for i in range(3)
    ]
    panels = PanelsConfig(
        panels=[PanelEntry(id="LOGISTICS-A", division=DivisionCode.LOGISTICS, room="R1")]
    )

    rows = compute_capacity_advisor(applicants, panels, grid, target_utilisation=0.83)
    logistics = next(r for r in rows if r.division == DivisionCode.LOGISTICS)

    assert logistics.demand == 3
    assert logistics.raw_supply == 3
    assert logistics.effective_supply == 3  # meets demand exactly, so not infeasible
    assert logistics.recommended_panels == 2  # ceil(3 / (3 * 0.83)) = 2
    assert logistics.panels_configured == 1
    assert logistics.verdict == "TIGHT"


def test_demand_exceeding_raw_supply_is_infeasible() -> None:
    grid = build_slot_grid(_event())
    all_slots = [s.slot_id for s in grid.slots]
    applicants = [
        _applicant(f"A{i}", DivisionCode.PROGRAM, DivisionCode.LIAISON, all_slots) for i in range(5)
    ]
    panels = PanelsConfig(
        panels=[PanelEntry(id="PROGRAM-A", division=DivisionCode.PROGRAM, room="R1")]
    )

    rows = compute_capacity_advisor(applicants, panels, grid, target_utilisation=0.83)
    program = next(r for r in rows if r.division == DivisionCode.PROGRAM)

    assert program.demand == 5
    assert program.raw_supply == 3  # 1 panel x 3 slots -- can never fit 5
    assert program.effective_supply == 3
    assert program.verdict == "INFEASIBLE"
    assert not is_feasible(rows)


def test_demand_with_no_configured_panels_is_infeasible() -> None:
    grid = build_slot_grid(_event())
    applicants = [_applicant("A1", DivisionCode.LIAISON, DivisionCode.FNB, [])]
    panels = PanelsConfig(panels=[])

    rows = compute_capacity_advisor(applicants, panels, grid, target_utilisation=0.83)
    liaison = next(r for r in rows if r.division == DivisionCode.LIAISON)

    assert liaison.panels_configured == 0
    assert liaison.raw_supply == 0
    assert liaison.verdict == "INFEASIBLE"


def test_panel_with_no_demand_is_ok() -> None:
    grid = build_slot_grid(_event())
    panels = PanelsConfig(panels=[PanelEntry(id="FNB-A", division=DivisionCode.FNB, room="R1")])

    rows = compute_capacity_advisor([], panels, grid, target_utilisation=0.83)
    fnb = next(r for r in rows if r.division == DivisionCode.FNB)

    assert fnb.demand == 0
    assert fnb.verdict == "OK"


def test_same_parent_pair_counts_twice_against_the_shared_division() -> None:
    """E-01: an applicant with both choices under one parent division demands
    two interviews from that division, not one (SPEC.md §1.2 Finding B)."""
    grid = build_slot_grid(_event())
    all_slots = [s.slot_id for s in grid.slots]
    applicants = [_applicant("A1", DivisionCode.MEDMARDOC, DivisionCode.MEDMARDOC, all_slots)]
    panels = PanelsConfig(
        panels=[PanelEntry(id="MEDMARDOC-A", division=DivisionCode.MEDMARDOC, room="R1")]
    )

    rows = compute_capacity_advisor(applicants, panels, grid, target_utilisation=0.83)
    medmardoc = next(r for r in rows if r.division == DivisionCode.MEDMARDOC)
    assert medmardoc.demand == 2


def test_panel_active_window_restricts_raw_supply() -> None:
    """FR-25: a panel's active_windows limits which slots count toward its supply."""
    grid = build_slot_grid(_event())
    panels = PanelsConfig(
        panels=[
            PanelEntry(
                id="CREATIVE-A",
                division=DivisionCode.CREATIVE,
                room="R1",
                active_windows=[ActiveWindow(date=DAY, start=time(18, 0), end=time(18, 20))],
            )
        ]
    )

    rows = compute_capacity_advisor([], panels, grid, target_utilisation=0.83)
    creative = next(r for r in rows if r.division == DivisionCode.CREATIVE)
    assert creative.raw_supply == 1  # only the 18:00-18:20 slot fits inside the window


def test_is_feasible_true_when_nothing_infeasible() -> None:
    grid = build_slot_grid(_event())
    panels = PanelsConfig(panels=[PanelEntry(id="FNB-A", division=DivisionCode.FNB, room="R1")])
    rows = compute_capacity_advisor([], panels, grid, target_utilisation=0.83)
    assert is_feasible(rows)


# ---- against the committed baseline config (SPEC.md §1.2 Finding A) ----


def test_committed_baseline_is_one_panel_per_division_per_day() -> None:
    """SPEC.md §1.2: the baseline is 12 panels — one per division per evening —
    that the load-balancer grows from. Two panels of one division never share
    a room on a day, even in the committed config."""
    settings = load_settings()
    by_division = Counter(p.division for p in settings.panels.panels)
    assert len(settings.panels.panels) == 12
    assert set(by_division.values()) == {2}  # exactly one Thursday + one Friday each
    assert _same_division_room_day_collisions(settings) == []


def test_baseline_is_grown_by_the_load_balancer_to_cover_an_even_40_split() -> None:
    """Finding A's worked example: 40 interviews per division, spread evenly.
    The one-per-division-per-day baseline is INFEASIBLE for it on its own;
    `rebalance_panels` spreads each hot division across more rooms until the
    Capacity Advisor clears."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    all_slots = [s.slot_id for s in grid.slots]

    applicants = [
        _applicant(f"C{i}", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, all_slots)
        for i in range(40)
    ] + [
        _applicant(f"P{i}", DivisionCode.PROGRAM, DivisionCode.LIAISON, all_slots)
        for i in range(40)
    ]

    before = compute_capacity_advisor(
        applicants, settings.panels, grid, settings.solver.target_utilisation, rooms=settings.rooms
    )
    assert not is_feasible(before)

    scaled, messages = rebalance_panels(settings, applicants, grid)
    assert messages
    after = compute_capacity_advisor(
        applicants, scaled.panels, grid, settings.solver.target_utilisation, rooms=settings.rooms
    )
    assert is_feasible(after)
    assert _same_division_room_day_collisions(scaled) == []


def _same_division_room_day_collisions(settings) -> list[tuple[str, str, str]]:
    """(division, date, room) tuples where two panels of one division are put in
    the same room on the same day — the thing Part 1 forbids in the auto
    panel-to-room assignment. Empty list == clean."""
    all_dates = {d.date for d in settings.event.days}
    room_days = {r.id: (set(r.days) if r.days else set(all_dates)) for r in settings.rooms.rooms}
    seen: set[tuple[DivisionCode, object, str]] = set()
    collisions: list[tuple[str, str, str]] = []
    for panel in settings.panels.panels:
        days = room_days.get(panel.room, set(all_dates))
        if panel.active_windows:
            days = days & {w.date for w in panel.active_windows}
        for day in days & all_dates:
            key = (panel.division, day, panel.room)
            if key in seen:
                collisions.append((panel.division.value, day.isoformat(), panel.room))
            seen.add(key)
    return collisions


def test_autoscale_panels_clears_an_infeasible_division() -> None:
    """A division whose demand outstrips the baseline gets extra panels — each
    in its own distinct room that evening — until the Advisor stops flagging
    it, and the caller is told."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    all_slots = [s.slot_id for s in grid.slots]

    # 25 applicants wanting LIAISON twice (50 interviews) — well past the
    # one-per-day baseline, but a shortfall spare distinct rooms can absorb.
    applicants = [
        _applicant(f"L{i}", DivisionCode.LIAISON, DivisionCode.LIAISON, all_slots)
        for i in range(25)
    ]

    before = compute_capacity_advisor(
        applicants, settings.panels, grid, settings.solver.target_utilisation, rooms=settings.rooms
    )
    assert not is_feasible(before)

    scaled, messages = autoscale_panels(settings, applicants, grid)

    assert any("Auto-scaled LIAISON" in m for m in messages)
    assert len(scaled.panels.panels) > len(settings.panels.panels)
    after = compute_capacity_advisor(
        applicants, scaled.panels, grid, settings.solver.target_utilisation, rooms=settings.rooms
    )
    assert is_feasible(after)
    # Every added panel is in its own room that evening.
    assert _same_division_room_day_collisions(scaled) == []
    # Untouched when nothing is short.
    ok_settings, ok_messages = autoscale_panels(settings, [], grid)
    assert ok_messages == []
    assert ok_settings is settings


def test_autoscale_flags_a_genuine_room_shortage_instead_of_stacking() -> None:
    """When a division needs more panels than there are rooms open that
    evening, autoscale fills every room once, then stops and says so — it does
    not keep stacking panels the solver could never run."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    all_slots = [s.slot_id for s in grid.slots]

    # 120 applicants wanting LIAISON twice = 240 interviews across two evenings:
    # unreachable with the rooms on hand.
    applicants = [
        _applicant(f"L{i}", DivisionCode.LIAISON, DivisionCode.LIAISON, all_slots)
        for i in range(120)
    ]

    scaled, messages = autoscale_panels(settings, applicants, grid)

    assert any("every room open that evening is full" in m and "LIAISON" in m for m in messages)
    # No same-division room doubling: it filled distinct rooms and then stopped.
    assert _same_division_room_day_collisions(scaled) == []


# ---- proactive load-balancing split (SPEC.md §5.5) ----


_rebalance_msg = re.compile(
    r"^Rebalanced [A-Z]+ \(.+\): (\d+)→(\d+) panels, ~(\d+) applicants each$"
)


def _creative_panels(settings) -> int:
    return sum(1 for p in settings.panels.panels if p.division == DivisionCode.CREATIVE)


def _rooms_open(settings, division: DivisionCode, day: date) -> int:
    all_dates = {d.date for d in settings.event.days}
    return sum(
        1
        for r in settings.rooms.rooms
        if division in r.divisions and day in (set(r.days) if r.days else all_dates)
    )


def test_rebalance_splits_before_panels_pack_past_the_threshold() -> None:
    """A hot division gets an extra panel — in a fresh room that evening — one
    at a time, with the whole per-day load re-spread across all of them
    (~N/each), not dumped on the newcomer."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    all_slots = [s.slot_id for s in grid.slots]

    # 25 applicants, both choices CREATIVE (E-01 same-parent pair) => 50
    # CREATIVE interviews, ~25 per evening — well past one panel per evening.
    applicants = [
        _applicant(f"C{i}", DivisionCode.CREATIVE, DivisionCode.CREATIVE, all_slots)
        for i in range(25)
    ]

    scaled, messages = rebalance_panels(settings, applicants, grid)

    # `rebalance_panels` also appends a "Balanced panel rooms" line; the split
    # log is the subset that matches the rebalance format.
    split_messages = [m for m in messages if _rebalance_msg.match(m)]
    assert split_messages, "expected at least one rebalance split"
    assert all(m.startswith("Rebalanced CREATIVE") for m in split_messages)
    parsed = [_rebalance_msg.match(m) for m in split_messages]
    steps = [(int(p.group(1)), int(p.group(2)), int(p.group(3))) for p in parsed]
    for before, after, each in steps:
        assert after == before + 1  # one panel at a time, per evening
        assert each == round(25 / after)  # even re-spread of that evening's ~25

    # Each evening's panel count climbs 1 -> 2 -> 3 ..., never skipping.
    for day_label in ("Thu", "Fri"):
        befores = [
            b for (b, _a, _e), m in zip(steps, split_messages, strict=True) if f"({day_label})" in m
        ]
        assert befores == list(range(1, 1 + len(befores)))

    assert _same_division_room_day_collisions(scaled) == []

    # An even split now sits under 85%, so a second pass is a no-op.
    again, again_messages = rebalance_panels(scaled, applicants, grid)
    assert again_messages == []
    assert again is scaled


def test_rebalance_is_capped_at_one_panel_per_room_per_evening() -> None:
    """Runaway same-day demand can't grow a division past one panel in every
    room open that evening (req. 4) — beyond that it is a genuine room
    shortage, not something the load-balancer papers over."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    all_slots = [s.slot_id for s in grid.slots]

    thu, fri = (d.date for d in settings.event.days)
    ceiling = _rooms_open(settings, DivisionCode.CREATIVE, thu) + _rooms_open(
        settings, DivisionCode.CREATIVE, fri
    )

    # Absurd demand: 90 applicants * 2 CREATIVE choices = 180 interviews.
    applicants = [
        _applicant(f"C{i}", DivisionCode.CREATIVE, DivisionCode.CREATIVE, all_slots)
        for i in range(90)
    ]

    scaled, messages = rebalance_panels(settings, applicants, grid)
    assert messages
    assert _creative_panels(scaled) == ceiling
    assert _same_division_room_day_collisions(scaled) == []
    assert REBALANCE_THRESHOLD == 0.85


def test_rebalance_leaves_a_comfortable_division_untouched() -> None:
    """When one panel per evening already absorbs the per-day load under 85%,
    nothing is added and the settings pass straight through."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    all_slots = [s.slot_id for s in grid.slots]

    # 8 applicants CREATIVE + PROGRAM: ~4 interviews per division per evening
    # against one 10-slot panel — comfortably under threshold.
    applicants = [
        _applicant(f"X{i}", DivisionCode.CREATIVE, DivisionCode.PROGRAM, all_slots)
        for i in range(8)
    ]

    scaled, messages = rebalance_panels(settings, applicants, grid)
    assert messages == []
    assert scaled is settings


# ---- room concurrency hard ceiling (Part 1) ----


def test_committed_rooms_stay_within_the_hard_concurrency_ceiling() -> None:
    """Part 1: every committed room allows 1..4 concurrent panels, and 4 is the
    ceiling. Consistent values, no accidental low caps."""
    settings = load_settings()
    caps = {r.id: r.max_concurrent_panels for r in settings.rooms.rooms}
    assert caps, "expected rooms in the committed config"
    for room_id, cap in caps.items():
        assert 1 <= cap <= ROOM_CONCURRENCY_CEILING, (room_id, cap)
    assert max(caps.values()) == ROOM_CONCURRENCY_CEILING


def test_rooms_config_rejects_a_concurrency_above_the_ceiling() -> None:
    """A rooms.yaml value over 4 fails at load, not deep in the solver
    (CLAUDE.md: fail loudly on malformed config)."""
    with pytest.raises(ValidationError):
        RoomsConfig.model_validate(
            {
                "rooms": [
                    {"id": "X", "max_concurrent_panels": 5, "divisions": ["FNB"]},
                ]
            }
        )


# ---- near-empty panel consolidation (Part 2) ----


def test_consolidate_merges_a_near_empty_added_panel_into_its_sibling() -> None:
    """A load-balanced panel (`origin="balanced"`) an even split would fill to
    only ~2 interviews is folded back into its same-division sibling when that
    sibling can absorb the load under the rebalance threshold — freeing the
    room, keeping the lower-numbered one."""
    base = load_settings()
    grid = build_slot_grid(base.event)
    thu = base.event.days[0].date
    thu_slots = [s.slot_id for s in grid.slots if s.date == thu]

    # Baseline FNB-A1 (room 2020) + an over-eager extra FNB panel that Thursday.
    panels = list(base.panels.panels) + [
        PanelEntry(
            id="FNB-A2",
            division=DivisionCode.FNB,
            room="3013",
            active_windows=[ActiveWindow(date=thu, start=time(18, 30), end=time(21, 30))],
            origin="balanced",
        )
    ]
    settings = base.model_copy(update={"panels": PanelsConfig(panels=panels)})

    # 4 FNB interviews, Thursday only — an even split is 2 per panel.
    applicants = [
        _applicant(f"F{i}", DivisionCode.FNB, DivisionCode.PROGRAM, thu_slots) for i in range(4)
    ]

    consolidated, messages = consolidate_panels(settings, applicants, grid)

    assert any(m.startswith("Consolidated FNB (Thu)") for m in messages)
    ids = {p.id for p in consolidated.panels.panels}
    assert "FNB-A2" not in ids  # the near-empty added panel is gone
    assert "FNB-A1" in ids  # the committed baseline panel is kept


def test_consolidate_leaves_a_busy_added_panel_alone() -> None:
    """When siblings can't absorb the small panel's load without blowing past
    the threshold, the panel stays — consolidation is a soft optimisation, it
    never forces an invalid merge."""
    base = load_settings()
    grid = build_slot_grid(base.event)
    thu = base.event.days[0].date
    thu_slots = [s.slot_id for s in grid.slots if s.date == thu]

    panels = list(base.panels.panels) + [
        PanelEntry(
            id="FNB-A2",
            division=DivisionCode.FNB,
            room="3013",
            active_windows=[ActiveWindow(date=thu, start=time(18, 30), end=time(21, 30))],
            origin="balanced",
        )
    ]
    settings = base.model_copy(update={"panels": PanelsConfig(panels=panels)})

    # 16 FNB interviews on a 9-slot Thursday: one panel alone is 16/9 ~ 178%,
    # far over threshold, so the second panel must survive.
    applicants = [
        _applicant(f"F{i}", DivisionCode.FNB, DivisionCode.PROGRAM, thu_slots) for i in range(16)
    ]

    consolidated, messages = consolidate_panels(settings, applicants, grid)

    assert messages == []
    assert consolidated is settings
    assert any(p.id == "FNB-A2" for p in consolidated.panels.panels)


def test_consolidate_never_removes_a_committed_baseline_panel() -> None:
    """Only load-balancer-added panels (`origin="balanced"`) are removable;
    a division whose only panel that evening is the baseline is left as-is even
    when its load is tiny."""
    base = load_settings()
    grid = build_slot_grid(base.event)
    thu = base.event.days[0].date
    thu_slots = [s.slot_id for s in grid.slots if s.date == thu]

    applicants = [
        _applicant(f"F{i}", DivisionCode.FNB, DivisionCode.PROGRAM, thu_slots) for i in range(2)
    ]

    consolidated, messages = consolidate_panels(base, applicants, grid)

    assert messages == []
    assert consolidated is base
    assert any(p.id == "FNB-A1" for p in consolidated.panels.panels)


def test_consolidate_folds_a_panel_an_even_split_would_not_flag_as_near_empty() -> None:
    """A balanced panel whose same-division panels — across every room — can
    still absorb the whole evening under the threshold is folded even when an
    even split across the panels looks comfortably balanced (3+ interviews
    each). The trigger is global spare capacity, not the per-panel headcount an
    even split implies."""
    base = load_settings()
    grid = build_slot_grid(base.event)
    thu = base.event.days[0].date
    thu_slots = [s.slot_id for s in grid.slots if s.date == thu]
    n_thu = len(thu_slots)

    panels = list(base.panels.panels) + [
        PanelEntry(
            id="FNB-A2",
            division=DivisionCode.FNB,
            room="3013",
            active_windows=[ActiveWindow(date=thu, start=time(18, 30), end=time(21, 30))],
            origin="balanced",
        )
    ]
    settings = base.model_copy(update={"panels": PanelsConfig(panels=panels)})

    # Demand one interview short of a single full-day panel at the threshold:
    # an even split across the two panels is ~3 each — well past the old
    # "1-2 interviews" near-empty bar — yet FNB-A1 alone still sits under it.
    demand = int(n_thu * REBALANCE_THRESHOLD) - 1
    applicants = [
        _applicant(f"F{i}", DivisionCode.FNB, DivisionCode.PROGRAM, thu_slots)
        for i in range(demand)
    ]

    consolidated, messages = consolidate_panels(settings, applicants, grid)

    assert any(m.startswith("Consolidated FNB (Thu)") for m in messages)
    assert not any(p.id == "FNB-A2" for p in consolidated.panels.panels)
    assert any(p.id == "FNB-A1" for p in consolidated.panels.panels)


# ---- per-room panel-count balancing ----


def _panels_per_room_on_day(settings, day: date) -> dict[str, int]:
    all_dates = {d.date for d in settings.event.days}
    room_days = {r.id: (set(r.days) if r.days else set(all_dates)) for r in settings.rooms.rooms}
    counts: Counter[str] = Counter()
    for p in settings.panels.panels:
        days = room_days.get(p.room, set(all_dates))
        if p.active_windows:
            days = days & {w.date for w in p.active_windows}
        if day in days & all_dates:
            counts[p.room] += 1
    return dict(counts)


def test_balance_panel_rooms_evens_out_a_stacked_evening() -> None:
    """`_pick_panel_room`'s sequential fill can pile several load-balanced
    panels into the first room while a room whose only panel is a lone baseline
    is left alone. The balancing pass moves the movable ones out until no room
    is 2+ panels heavier than another, never breaching the ceiling or
    room-exclusivity, and never relocating a committed baseline panel."""
    base = load_settings()
    grid = build_slot_grid(base.event)
    thu = base.event.days[0].date
    win = [ActiveWindow(date=thu, start=time(18, 30), end=time(21, 30))]

    stacked = list(base.panels.panels) + [
        PanelEntry(id=pid, division=div, room="2016", active_windows=win, origin="balanced")
        for pid, div in [
            ("FNB-A2", DivisionCode.FNB),
            ("LOGISTICS-A2", DivisionCode.LOGISTICS),
            ("MEDMARDOC-A2", DivisionCode.MEDMARDOC),
            ("LIAISON-A2", DivisionCode.LIAISON),
        ]
    ]
    settings = base.model_copy(update={"panels": PanelsConfig(panels=stacked)})
    assert _panels_per_room_on_day(settings, thu)["2016"] == 5

    balanced, messages = balance_panel_rooms(settings, grid)

    counts = _panels_per_room_on_day(balanced, thu)
    assert max(counts.values()) - min(counts.values()) <= 1
    assert max(counts.values()) <= ROOM_CONCURRENCY_CEILING
    assert _same_division_room_day_collisions(balanced) == []
    assert any(m.startswith("Balanced panel rooms") for m in messages)

    baseline_room = {p.id: p.room for p in base.panels.panels}
    for p in balanced.panels.panels:
        if p.origin == "config":
            assert p.room == baseline_room[p.id]

    # Idempotent: a second pass over an already-even set moves nothing.
    again, again_messages = balance_panel_rooms(balanced, grid)
    assert again is balanced
    assert again_messages == []
