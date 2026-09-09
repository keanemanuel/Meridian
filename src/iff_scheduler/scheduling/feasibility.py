"""Capacity Advisor — demand vs. panel-slot supply per division, run before
the solver (SPEC.md §5.5; §1.2 Finding A).

Finding A is explicit that raw panel*slot capacity assumes 100% utilisation
and is "unreachable in practice" — it names 83% as the realistic baseline.
So the hard-stop here is driven by `effective_supply` (which discounts for
when applicants actually said they're free), not `raw_supply` (the
unrealistic theoretical ceiling, shown for context only). This turns
"the solver failed" into "spawn N more panels for division D" before a
single interview is placed (SPEC.md §4.2 Stage 5).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date as Date
from math import ceil
from typing import Literal

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import SlotGrid
from iff_scheduler.domain.models import Applicant
from iff_scheduler.settings import (
    ActiveWindow,
    EventConfig,
    PanelEntry,
    PanelsConfig,
    RoomsConfig,
    Settings,
)

Verdict = Literal["OK", "TIGHT", "INFEASIBLE"]

AUTO_PANEL_TAG = "AUTO"


@dataclass(frozen=True)
class DivisionCapacity:
    """One row of the Capacity Advisor table."""

    division: DivisionCode
    demand: int
    panels_configured: int
    raw_supply: int
    effective_supply: int
    recommended_panels: int
    verdict: Verdict


def _panel_active_slot_ids(
    panel: PanelEntry, grid: SlotGrid, room_dates: set[Date] | None = None
) -> set[str]:
    """A panel with no declared active_windows is active for the whole event
    (FR-25), minus any day its room does not exist on (FR-20)."""
    slots = grid.slots if room_dates is None else [s for s in grid.slots if s.date in room_dates]
    if not panel.active_windows:
        return {slot.slot_id for slot in slots}
    ids: set[str] = set()
    for slot in slots:
        for window in panel.active_windows:
            if (
                window.date == slot.date
                and window.start <= slot.start_time
                and slot.end_time <= window.end
            ):
                ids.add(slot.slot_id)
                break
    return ids


def _demand_by_division(applicants: list[Applicant]) -> Counter[DivisionCode]:
    """Each applicant contributes one interview to each of their choices'
    parent divisions — including twice to the same division for a same-parent
    pair (SPEC.md §1.2 Finding B, E-01), or just once for a single-choice
    applicant (`division_2 is None`)."""
    demand: Counter[DivisionCode] = Counter()
    for applicant in applicants:
        demand[applicant.division_1] += 1
        if applicant.division_2 is not None:
            demand[applicant.division_2] += 1
    return demand


def _room_dates(rooms: RoomsConfig | None, grid: SlotGrid) -> dict[str, set[Date]]:
    all_dates = {slot.date for slot in grid.slots}
    if rooms is None:
        return {}
    return {r.id: (set(r.days) if r.days else set(all_dates)) for r in rooms.rooms}


def compute_capacity_advisor(
    applicants: list[Applicant],
    panels: PanelsConfig,
    grid: SlotGrid,
    target_utilisation: float,
    rooms: RoomsConfig | None = None,
) -> list[DivisionCapacity]:
    """Build the per-division demand/supply table (SPEC.md §5.5).

    When `rooms` is given, a panel's supply is capped to the days its room is
    available on (FR-20) — matching what the solver will actually see.
    """
    demand = _demand_by_division(applicants)
    room_dates = _room_dates(rooms, grid)

    panels_by_division: dict[DivisionCode, list[PanelEntry]] = defaultdict(list)
    for panel in panels.panels:
        panels_by_division[panel.division].append(panel)

    # A division with demand but no panels, or panels but no demand, still
    # gets a row rather than silently vanishing from the table.
    divisions = set(demand) | set(panels_by_division)
    total_slots = len(grid.slots)

    rows: list[DivisionCapacity] = []
    for division in sorted(divisions, key=lambda d: d.value):
        division_demand = demand.get(division, 0)
        division_panels = panels_by_division.get(division, [])

        active_by_panel = [
            _panel_active_slot_ids(panel, grid, room_dates.get(panel.room))
            for panel in division_panels
        ]
        raw_supply = sum(len(ids) for ids in active_by_panel)

        applicants_by_slot: Counter[str] = Counter()
        for applicant in applicants:
            if applicant.division_1 != division and applicant.division_2 != division:
                continue
            for slot_id in applicant.availability_slots:
                applicants_by_slot[slot_id] += 1

        effective_supply = 0
        for slot in grid.slots:
            panels_active_here = sum(1 for ids in active_by_panel if slot.slot_id in ids)
            if panels_active_here == 0:
                continue
            effective_supply += min(panels_active_here, applicants_by_slot.get(slot.slot_id, 0))

        recommended_panels = (
            ceil(division_demand / (total_slots * target_utilisation)) if division_demand > 0 else 0
        )

        verdict: Verdict
        if division_demand == 0:
            verdict = "OK"
        elif effective_supply < division_demand:
            verdict = "INFEASIBLE"
        elif len(division_panels) < recommended_panels:
            verdict = "TIGHT"
        else:
            verdict = "OK"

        rows.append(
            DivisionCapacity(
                division=division,
                demand=division_demand,
                panels_configured=len(division_panels),
                raw_supply=raw_supply,
                effective_supply=effective_supply,
                recommended_panels=recommended_panels,
                verdict=verdict,
            )
        )

    return rows


def is_feasible(rows: list[DivisionCapacity]) -> bool:
    return all(row.verdict != "INFEASIBLE" for row in rows)


def _all_day_windows(event: EventConfig) -> list[ActiveWindow]:
    """One active window per event day covering its full opening hours — used
    to make an auto-scaled panel active on every evening."""
    return [ActiveWindow(date=d.date, start=d.start, end=d.end) for d in event.days]


def _autoscale_room(rooms: RoomsConfig, event: EventConfig, division: DivisionCode) -> str:
    """Pick the room an auto-scaled panel for `division` should sit in: the
    first room that accepts the division AND runs every event day, so the
    panel really is active both evenings. Falls back to the first room that
    accepts the division, then to the first room at all."""
    event_dates = {d.date for d in event.days}
    accepts = [r for r in rooms.rooms if division in r.divisions]
    both_days = [r for r in accepts if not r.days or set(r.days) >= event_dates]
    for candidates in (both_days, accepts, list(rooms.rooms)):
        if candidates:
            return candidates[0].id
    raise ValueError("rooms.yaml defines no rooms — cannot auto-scale panels.")


def autoscale_panels(
    settings: Settings,
    applicants: list[Applicant],
    grid: SlotGrid,
    *,
    max_rounds: int = 12,
) -> tuple[Settings, list[str]]:
    """Add panels for any INFEASIBLE division until the Capacity Advisor
    clears, so the solver can still place every interview (CLAUDE.md
    invariant 1 — two interviews per applicant, never relaxed).

    This scales *capacity* to the Advisor's own `recommended_panels`; it
    never touches applicant data or relaxes a hard constraint (invariant 3).
    Every panel added is real staffing the committee still has to supply, so
    the returned messages ("Auto-scaled PROGRAM: added 2 panel(s)") are meant
    to surface as a review-your-staffing warning, not be swallowed.

    Returns the (possibly unchanged) settings and the list of messages —
    empty when nothing was added.
    """
    windows = _all_day_windows(settings.event)
    panels: list[PanelEntry] = list(settings.panels.panels)
    added: Counter[DivisionCode] = Counter()

    for _ in range(max_rounds):
        rows = compute_capacity_advisor(
            applicants,
            PanelsConfig(panels=panels),
            grid,
            settings.solver.target_utilisation,
            rooms=settings.rooms,
        )
        short = [r for r in rows if r.verdict == "INFEASIBLE"]
        if not short:
            break
        for row in short:
            need = max(1, row.recommended_panels - row.panels_configured)
            room_id = _autoscale_room(settings.rooms, settings.event, row.division)
            for _n in range(need):
                added[row.division] += 1
                panels.append(
                    PanelEntry(
                        id=f"{row.division.value}-{AUTO_PANEL_TAG}-{added[row.division]}",
                        division=row.division,
                        room=room_id,
                        active_windows=windows,
                    )
                )

    if not added:
        return settings, []

    augmented = settings.model_copy(update={"panels": PanelsConfig(panels=panels)})
    messages = [
        f"Auto-scaled {division.value}: added {count} panel(s)"
        for division, count in sorted(added.items(), key=lambda kv: kv[0].value)
    ]
    return augmented, messages
