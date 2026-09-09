"""Tests for scheduling/feasibility.py — the Capacity Advisor (SPEC.md §5.5,
§1.2 Finding A)."""

from __future__ import annotations

import re
from datetime import date, datetime, time

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.domain.models import Applicant
from iff_scheduler.scheduling.feasibility import (
    REBALANCE_THRESHOLD,
    autoscale_panels,
    compute_capacity_advisor,
    is_feasible,
    rebalance_panels,
)
from iff_scheduler.settings import (
    ActiveWindow,
    DayConfig,
    EventConfig,
    PanelEntry,
    PanelsConfig,
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


def test_baseline_config_covers_an_even_40_per_division_split() -> None:
    """Finding A's worked example: demand split perfectly evenly (40 per
    division). panels.yaml is demand-weighted (PROGRAM 6, CREATIVE 4); at
    23 slots x 0.83 the formula recommends 3 for a 40-demand division, so
    every division clears the bar with headroom and none is INFEASIBLE."""
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

    rows = compute_capacity_advisor(
        applicants,
        settings.panels,
        grid,
        settings.solver.target_utilisation,
        rooms=settings.rooms,
    )
    by_division = {r.division: r for r in rows}
    # Demand-weighted committed counts.
    assert by_division[DivisionCode.PROGRAM].panels_configured == 6
    assert by_division[DivisionCode.MEDMARDOC].panels_configured == 5
    assert by_division[DivisionCode.CREATIVE].panels_configured == 4
    assert by_division[DivisionCode.LIAISON].panels_configured == 3
    # The hot divisions clear an even 40-way split outright; a thinner
    # division (LIAISON: 3 panels by design) is what autoscale is for.
    assert by_division[DivisionCode.PROGRAM].verdict == "OK"
    assert by_division[DivisionCode.CREATIVE].verdict == "OK"


def test_autoscale_panels_clears_an_infeasible_division() -> None:
    """A division whose demand outstrips the committed config gets extra
    panels until the Advisor stops flagging it — and the caller is told."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    all_slots = [s.slot_id for s in grid.slots]

    # 120 applicants all wanting LIAISON twice — far past its 3 panels.
    applicants = [
        _applicant(f"L{i}", DivisionCode.LIAISON, DivisionCode.LIAISON, all_slots)
        for i in range(120)
    ]

    before = compute_capacity_advisor(
        applicants, settings.panels, grid, settings.solver.target_utilisation, rooms=settings.rooms
    )
    assert not is_feasible(before)

    scaled, messages = autoscale_panels(settings, applicants, grid)

    assert any("LIAISON" in m for m in messages)
    assert len(scaled.panels.panels) > len(settings.panels.panels)
    after = compute_capacity_advisor(
        applicants, scaled.panels, grid, settings.solver.target_utilisation, rooms=settings.rooms
    )
    assert is_feasible(after)
    # Untouched when nothing is short.
    ok_settings, ok_messages = autoscale_panels(settings, [], grid)
    assert ok_messages == []
    assert ok_settings is settings


# ---- proactive load-balancing split (SPEC.md §5.5, req: rebalance not overflow) ----


_rebalance_msg = re.compile(
    r"^Rebalanced [A-Z]+ \(.+\): (\d+)→(\d+) panels, ~(\d+) applicants each$"
)


def _creative_panels(settings) -> int:
    return sum(1 for p in settings.panels.panels if p.division == DivisionCode.CREATIVE)


def _both_days_concurrency(settings, division: DivisionCode) -> int:
    event_dates = {d.date for d in settings.event.days}
    return sum(
        r.max_concurrent_panels
        for r in settings.rooms.rooms
        if division in r.divisions and (not r.days or set(r.days) >= event_dates)
    )


def test_rebalance_splits_before_panels_pack_past_the_threshold() -> None:
    """A division whose even per-day split would sit above 85% gets an extra
    panel, and the whole load is re-spread across ALL panels (~N/each), not
    dumped on the new one."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    all_slots = [s.slot_id for s in grid.slots]

    creative_before = _creative_panels(settings)
    # 25 applicants, both choices CREATIVE (E-01 same-parent pair) => 50
    # CREATIVE interviews demanded each day, well past what the committed
    # panels seat at 85%, but nothing else is touched.
    applicants = [
        _applicant(f"C{i}", DivisionCode.CREATIVE, DivisionCode.CREATIVE, all_slots)
        for i in range(25)
    ]

    scaled, messages = rebalance_panels(settings, applicants, grid)

    assert messages, "expected at least one rebalance split"
    assert all("Rebalanced CREATIVE" in m for m in messages)
    parsed = [_rebalance_msg.match(m) for m in messages]
    assert all(p is not None for p in parsed), messages
    # Panel counts step up one at a time: 4->5, 5->6, ...
    steps = [(int(p.group(1)), int(p.group(2)), int(p.group(3))) for p in parsed]
    assert steps[0][0] == creative_before
    for before, after, each in steps:
        assert after == before + 1
        # ~even split: reported per-panel load is total / new panel count,
        # not a near-zero "just the newcomers" figure.
        assert each == round(50 / after)
    afters = [s[1] for s in steps]
    assert afters == list(range(creative_before + 1, creative_before + 1 + len(steps)))

    scaled_creative = _creative_panels(scaled)
    assert scaled_creative == creative_before + len(messages)
    # An even split now sits under the threshold, so a second pass is a no-op
    # and returns the same object untouched (idempotent).
    again, again_messages = rebalance_panels(scaled, applicants, grid)
    assert again_messages == []
    assert again is scaled


def test_rebalance_is_capped_so_it_cannot_grow_unbounded() -> None:
    """Runaway same-day demand can't grow a division past what the rooms that
    run every evening can seat at once (req. 4)."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    all_slots = [s.slot_id for s in grid.slots]

    creative_before = _creative_panels(settings)
    ceiling = creative_before + _both_days_concurrency(settings, DivisionCode.CREATIVE)

    # Absurd demand: 90 applicants * 2 CREATIVE choices = 180/day.
    applicants = [
        _applicant(f"C{i}", DivisionCode.CREATIVE, DivisionCode.CREATIVE, all_slots)
        for i in range(90)
    ]

    scaled, messages = rebalance_panels(settings, applicants, grid)
    assert messages
    assert _creative_panels(scaled) == ceiling
    # It stopped at the cap despite the load still being way over threshold.
    assert len(messages) == ceiling - creative_before


def test_rebalance_leaves_a_comfortable_division_untouched() -> None:
    """When the committed panels already absorb the per-day load with slack
    under 85%, nothing is added and the settings pass straight through."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    all_slots = [s.slot_id for s in grid.slots]

    # 15 applicants CREATIVE + PROGRAM: both divisions stay comfortably under
    # 85% even if their whole load lands on the shorter (Thursday) evening.
    applicants = [
        _applicant(f"X{i}", DivisionCode.CREATIVE, DivisionCode.PROGRAM, all_slots)
        for i in range(15)
    ]

    scaled, messages = rebalance_panels(settings, applicants, grid)
    assert messages == []
    assert scaled is settings
    assert REBALANCE_THRESHOLD == 0.85
