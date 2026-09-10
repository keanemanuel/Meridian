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

# Proactive load-balancing split (SPEC.md §5.5, §1.2 Finding A). When an even
# split of a division's per-day load would push its panels past this
# utilisation, add a panel and re-spread — rather than packing existing panels
# to ~100% and forcing later applicants into a clash. Hard-coded on purpose:
# it is a structural safety margin, not a per-event tuning knob.
REBALANCE_THRESHOLD = 0.85

BALANCE_PANEL_TAG = "BAL"


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


def _division_rooms_by_date(
    panels: list[PanelEntry],
    division: DivisionCode,
    room_dates: dict[str, set[Date]],
    event_dates: set[Date],
) -> dict[Date, set[str]]:
    """(event day) -> rooms already running a panel of `division` that day.

    Two panels of one division may never share a room on the same day: each is
    a separate interview station and stacking them in one room adds no real
    capacity. This is what `_pick_panel_room` checks before placing a new one.
    """
    taken: dict[Date, set[str]] = defaultdict(set)
    for panel in panels:
        if panel.division != division:
            continue
        room_days = room_dates.get(panel.room, set(event_dates))
        if panel.active_windows:
            active_days = {w.date for w in panel.active_windows} & room_days & event_dates
        else:
            active_days = room_days & event_dates
        for day in active_days:
            taken[day].add(panel.room)
    return taken


def _pick_panel_room(
    rooms: RoomsConfig,
    event: EventConfig,
    division: DivisionCode,
    existing_panels: list[PanelEntry],
    room_dates: dict[str, set[Date]],
) -> str | None:
    """Room for a newly added panel of `division` (rebalance or autoscale).

    A newly added panel is active for the whole event, so it runs on every
    evening its room is open. Pick the first room that (a) can host the
    division, (b) is open on at least one event day, and (c) is NOT already
    running another panel of the same division on any day it would be active —
    same-division panels are kept in distinct rooms so each one adds a real
    station. Prefer a room open every event day so the panel really is active
    both evenings.

    Returns None when every room that can host `division` is already taken by
    it: a genuine capacity ceiling for the caller to warn about, rather than
    silently doubling two panels of one division into one room.
    """
    event_dates = {d.date for d in event.days}
    taken = _division_rooms_by_date(existing_panels, division, room_dates, event_dates)

    accepts = [r for r in rooms.rooms if division in r.divisions]
    both_days = [r for r in accepts if not r.days or set(r.days) >= event_dates]
    for candidates in (both_days, accepts):
        for room in candidates:
            run_dates = (set(room.days) if room.days else set(event_dates)) & event_dates
            if run_dates and all(room.id not in taken.get(day, set()) for day in run_dates):
                return room.id
    if not rooms.rooms:
        raise ValueError("rooms.yaml defines no rooms — cannot add a panel.")
    return None


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
    empty when nothing was added and no capacity ceiling was hit. A ceiling
    warning is appended when a division needs more panels but every room that
    can host it already runs one (panels are never stacked to get past this).
    """
    windows = _all_day_windows(settings.event)
    room_dates = _room_dates(settings.rooms, grid)
    panels: list[PanelEntry] = list(settings.panels.panels)
    added: Counter[DivisionCode] = Counter()
    ceiling_hit: set[DivisionCode] = set()
    warnings: list[str] = []

    for _ in range(max_rounds):
        rows = compute_capacity_advisor(
            applicants,
            PanelsConfig(panels=panels),
            grid,
            settings.solver.target_utilisation,
            rooms=settings.rooms,
        )
        short = [r for r in rows if r.verdict == "INFEASIBLE" and r.division not in ceiling_hit]
        if not short:
            break
        progressed = False
        for row in short:
            need = max(1, row.recommended_panels - row.panels_configured)
            for _n in range(need):
                room_id = _pick_panel_room(
                    settings.rooms, settings.event, row.division, panels, room_dates
                )
                if room_id is None:
                    ceiling_hit.add(row.division)
                    warnings.append(
                        f"{row.division.value}: capacity ceiling — every room that can host "
                        f"{row.division.value} already runs a {row.division.value} panel on "
                        "every evening it is open. Two panels of one division are not stacked "
                        "in a room; add a room or an evening to place more."
                    )
                    break
                added[row.division] += 1
                progressed = True
                panels.append(
                    PanelEntry(
                        id=f"{row.division.value}-{AUTO_PANEL_TAG}-{added[row.division]}",
                        division=row.division,
                        room=room_id,
                        active_windows=windows,
                    )
                )
        if not progressed:
            break

    if not added:
        return settings, warnings

    augmented = settings.model_copy(update={"panels": PanelsConfig(panels=panels)})
    messages = [
        f"Auto-scaled {division.value}: added {count} panel(s)"
        for division, count in sorted(added.items(), key=lambda kv: kv[0].value)
    ]
    return augmented, messages + warnings


def _event_day_labels(event: EventConfig) -> dict[Date, str]:
    return {day.date: day.label for day in event.days}


def _slots_per_day(grid: SlotGrid, event: EventConfig) -> int:
    """Largest slot count on any single event day — the most panels of one
    division a room could ever keep busy in a day."""
    return max(
        (sum(1 for s in grid.slots if s.date == day.date) for day in event.days),
        default=0,
    )


def _demand_by_division_day(
    applicants: list[Applicant], grid: SlotGrid
) -> dict[tuple[DivisionCode, Date], int]:
    """(division, day) -> interviews that division owes applicants who could
    attend that day. An applicant free on both evenings is counted against
    both — deliberately conservative, so each division/day is sized for the
    case where its whole load lands on one day. The slack this leaves is the
    point (SPEC.md §1.2 Finding A): room to absorb late applicants without a
    clash, not a bug."""
    date_of_slot = {slot.slot_id: slot.date for slot in grid.slots}
    demand: Counter[tuple[DivisionCode, Date]] = Counter()
    for applicant in applicants:
        days = {date_of_slot[s] for s in applicant.availability_slots if s in date_of_slot}
        divisions = [applicant.division_1]
        if applicant.division_2 is not None:
            divisions.append(applicant.division_2)
        for division in divisions:
            for day in days:
                demand[(division, day)] += 1
    return demand


def _capacity_by_division_day(
    panels: list[PanelEntry], grid: SlotGrid, room_dates: dict[str, set[Date]]
) -> dict[tuple[DivisionCode, Date], int]:
    """(division, day) -> interview slots the currently allocated panels of
    that division can run that day."""
    slots_by_day: dict[Date, list[str]] = defaultdict(list)
    for slot in grid.slots:
        slots_by_day[slot.date].append(slot.slot_id)

    capacity: Counter[tuple[DivisionCode, Date]] = Counter()
    for panel in panels:
        active = _panel_active_slot_ids(panel, grid, room_dates.get(panel.room))
        for day, slot_ids in slots_by_day.items():
            here = sum(1 for sid in slot_ids if sid in active)
            if here:
                capacity[(panel.division, day)] += here
    return capacity


def _division_panel_cap(
    rooms: RoomsConfig,
    event: EventConfig,
    division: DivisionCode,
    base_count: int,
    slots_per_day: int,
) -> int:
    """Hard ceiling on panels for one division so rebalancing can't grow
    unbounded (req. 4). A rebalance panel is added all-event-days, so it can
    only ever be seated in a room that runs every evening (`_autoscale_room`);
    the extra panels are therefore capped at what those rooms can seat at once
    (FR-24). Never more than a day has slots, either — that buys nothing."""
    event_dates = {d.date for d in event.days}
    flexible = sum(
        r.max_concurrent_panels
        for r in rooms.rooms
        if division in r.divisions and (not r.days or set(r.days) >= event_dates)
    )
    if flexible <= 0:  # no both-days room accepts it — fall back to any room
        flexible = sum(r.max_concurrent_panels for r in rooms.rooms if division in r.divisions)
    ceiling = base_count + flexible
    return min(ceiling, max(slots_per_day, base_count)) if slots_per_day else ceiling


def rebalance_panels(
    settings: Settings,
    applicants: list[Applicant],
    grid: SlotGrid,
    *,
    threshold: float = REBALANCE_THRESHOLD,
    max_rounds: int = 64,
) -> tuple[Settings, list[str]]:
    """Proactively split a division's panels *before* the solve whenever an
    even split of its per-day load would still sit above `threshold`
    utilisation (default 85%), then re-spread the whole division/day load
    across every panel — existing and new — instead of packing the old panels
    and routing only new applicants to the new one.

    The decision made here is purely structural: how many panels each division
    needs so an even split has slack. CP-SAT then assigns individual
    applicants to slots, and its balance term (W_BALANCE) does the actual
    evening-out. Runs ahead of `autoscale_panels`, which stays the INFEASIBLE
    backstop.

    Returns the (possibly unchanged) settings and one solve-summary log line
    per split, e.g. "Rebalanced MEDMARDOC (Thu): 1->2 panels, ~50 applicants
    each" (SPEC.md §5.5).
    """
    rooms = settings.rooms
    labels = _event_day_labels(settings.event)
    room_dates = _room_dates(rooms, grid)
    windows = _all_day_windows(settings.event)
    slots_per_day = _slots_per_day(grid, settings.event)

    demand = _demand_by_division_day(applicants, grid)
    panels: list[PanelEntry] = list(settings.panels.panels)
    base_counts = Counter(panel.division for panel in panels)
    added: Counter[DivisionCode] = Counter()
    messages: list[str] = []
    # (division, day) pairs that want another panel but have no distinct room
    # left — a real capacity ceiling. Kept out of the candidate list so the
    # loop does not spin on them, and warned about once.
    ceiling_hit: set[tuple[DivisionCode, Date]] = set()

    for _ in range(max_rounds):
        capacity = _capacity_by_division_day(panels, grid, room_dates)
        counts = Counter(panel.division for panel in panels)

        # Pick the single most-overloaded (division, day) this round, add one
        # panel, then recompute from scratch so every day of that division
        # sees the wider set before the next split.
        candidates: list[tuple[float, str, Date, DivisionCode, int]] = []
        for (division, day), need in demand.items():
            if (division, day) in ceiling_hit:
                continue
            slot_cap = capacity.get((division, day), 0)
            if slot_cap <= 0 or need <= 0:
                continue
            util = need / slot_cap
            if util <= threshold:
                continue
            panel_cap = _division_panel_cap(
                rooms, settings.event, division, base_counts[division], slots_per_day
            )
            if counts[division] >= panel_cap:
                continue
            candidates.append((util, division.value, day, division, need))

        if not candidates:
            break
        candidates.sort(key=lambda c: (-c[0], c[1], c[2].isoformat()))
        _util, _name, day, division, need = candidates[0]

        # A split only helps if the new panel gets its own room: two panels of
        # one division in one room run no extra interviews. If none is free,
        # this is a genuine ceiling — say so rather than stacking.
        room_id = _pick_panel_room(rooms, settings.event, division, panels, room_dates)
        if room_id is None:
            ceiling_hit.add((division, day))
            messages.append(
                f"Cannot split {division.value} "
                f"({labels.get(day, day.isoformat())}): every room that can host "
                f"{division.value} already runs a {division.value} panel that evening. "
                "Capacity ceiling — add a room; panels were not stacked."
            )
            continue

        before = counts[division]
        after = before + 1
        added[division] += 1
        panels.append(
            PanelEntry(
                id=f"{division.value}-{BALANCE_PANEL_TAG}-{added[division]}",
                division=division,
                room=room_id,
                active_windows=windows,
            )
        )
        messages.append(
            f"Rebalanced {division.value} ({labels.get(day, day.isoformat())}): "
            f"{before}→{after} panels, ~{round(need / after)} applicants each"
        )

    if not added:
        return settings, messages

    augmented = settings.model_copy(update={"panels": PanelsConfig(panels=panels)})
    return augmented, messages
