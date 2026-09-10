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
    RoomEntry,
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


def _day_window(event: EventConfig, day: Date) -> ActiveWindow:
    """Full opening-hours window for one event day — pins an added panel to the
    single evening its capacity is needed on, so the per-day capacity maths
    (and the solver) credit it to that evening only."""
    match = next(d for d in event.days if d.date == day)
    return ActiveWindow(date=day, start=match.start, end=match.end)


def _panel_runs_on_day(
    panel: PanelEntry, day: Date, room_dates: dict[str, set[Date]], event_dates: set[Date]
) -> bool:
    """True when `panel` conducts interviews on `day`: its room is open then
    and, if it declares windows, one of them falls on that day."""
    if day not in room_dates.get(panel.room, set(event_dates)):
        return False
    return not panel.active_windows or any(w.date == day for w in panel.active_windows)


def _rooms_for_division_on_day(
    rooms: RoomsConfig,
    division: DivisionCode,
    day: Date,
    room_dates: dict[str, set[Date]],
    event_dates: set[Date],
) -> list[RoomEntry]:
    """Rooms that can host `division` and are open on `day`, in config order —
    the order the load-balancer consumes them in (Room 1, Room 2, ...)."""
    return [
        r
        for r in rooms.rooms
        if division in r.divisions and day in room_dates.get(r.id, set(event_dates))
    ]


def _pick_panel_room(
    rooms: RoomsConfig,
    event: EventConfig,
    division: DivisionCode,
    existing_panels: list[PanelEntry],
    room_dates: dict[str, set[Date]],
    day: Date,
) -> tuple[str, str | None]:
    """Room for a new `division` panel that runs on `day`.

    Fills the rooms open that evening one at a time, in config order: the first
    room not already running a `division` panel that day (and, preferably, with
    physical headroom). Different divisions freely share a room, bounded by its
    `max_concurrent_panels` (FR-24); two panels of the *same* division never
    share a room on a day — except the rare, loudly-logged fallback taken only
    when every room open that evening already runs a `division` panel, so the
    solver is still handed a placeable schedule (invariant 1).

    Returns `(room_id, note)`: `note` is None on the normal path, otherwise a
    line the caller must surface.
    """
    event_dates = {d.date for d in event.days}
    label = next((d.label for d in event.days if d.date == day), day.isoformat())

    running = [p for p in existing_panels if _panel_runs_on_day(p, day, room_dates, event_dates)]
    division_rooms = {p.room for p in running if p.division == division}
    load: Counter[str] = Counter(p.room for p in running)

    candidates = _rooms_for_division_on_day(rooms, division, day, room_dates, event_dates)
    if not candidates:
        raise ValueError(
            f"rooms.yaml has no room open on {day.isoformat()} that can host "
            f"{division.value} — cannot add a panel."
        )

    # Normal: a room with no same-division panel that evening and headroom.
    for room in candidates:
        if room.id not in division_rooms and load[room.id] < room.max_concurrent_panels:
            return room.id, None

    # Every headroom room already runs a `division` panel — take a room with no
    # `division` panel over its nominal cap (different divisions only; the
    # solver's C4 still limits how many interview at once), and note it.
    for room in candidates:
        if room.id not in division_rooms:
            return room.id, (
                f"Room {room.id} ({label}) now holds {load[room.id] + 1} panels vs a "
                f"configured max of {room.max_concurrent_panels} — different divisions; "
                "the solver still caps simultaneous interviews at the max."
            )

    # Fallback: every room open that evening already runs a `division` panel.
    room = min(candidates, key=lambda r: load[r.id])
    return room.id, (
        f"FALLBACK: a 2nd {division.value} panel was placed in room {room.id} ({label}) — "
        f"all {len(candidates)} rooms open that evening already run a {division.value} panel. "
        "This should be rare (~1%); if it fires at realistic applicant volume, re-check the "
        "ingested data and availability before staffing to it."
    )


def _try_add_capacity_panel(
    panels: list[PanelEntry],
    settings: Settings,
    grid: SlotGrid,
    room_dates: dict[str, set[Date]],
    division: DivisionCode,
    day: Date,
    panel_id: str,
) -> tuple[bool, str | None]:
    """Append one `division` panel active on `day`, keeping it only if it
    actually lifts that evening's usable capacity. A panel dropped into a room
    already at its `max_concurrent_panels` is a phantom the solver can never
    run (C4), so it is rolled back and the caller stops splitting that evening
    — this is what keeps the load-balancer from bloating the model when the
    rooms are physically full. Returns `(kept, note)`."""
    rooms = settings.rooms
    before = _capacity_by_division_day(panels, rooms, grid, room_dates).get((division, day), 0)
    room_id, note = _pick_panel_room(rooms, settings.event, division, panels, room_dates, day)
    panels.append(
        PanelEntry(
            id=panel_id,
            division=division,
            room=room_id,
            active_windows=[_day_window(settings.event, day)],
        )
    )
    after = _capacity_by_division_day(panels, rooms, grid, room_dates).get((division, day), 0)
    if after <= before:
        panels.pop()
        return False, None
    return True, note


def autoscale_panels(
    settings: Settings,
    applicants: list[Applicant],
    grid: SlotGrid,
    *,
    max_rounds: int = 32,
) -> tuple[Settings, list[str]]:
    """Add panels for any INFEASIBLE division — on the evening it is actually
    short — until the Capacity Advisor clears, so the solver can still place
    every interview (CLAUDE.md invariant 1). Runs after `rebalance_panels`.

    Each panel is seated by `_pick_panel_room`: a distinct room per
    same-division panel while one is free that evening, otherwise the
    loudly-logged same-room fallback so a schedule is always produced. It never
    touches applicant data or relaxes a hard constraint (invariant 3); every
    panel is real staffing to review, so the messages ("Auto-scaled PROGRAM:
    added 2 panel(s)", plus any fallback notes) are meant to surface.

    Returns the (possibly unchanged) settings and the messages — empty when
    nothing was added.
    """
    room_dates = _room_dates(settings.rooms, grid)
    labels = _event_day_labels(settings.event)
    demand_dd = _demand_by_division_day(applicants, grid)
    panels: list[PanelEntry] = list(settings.panels.panels)
    added: Counter[DivisionCode] = Counter()
    notes: list[str] = []
    saturated: set[tuple[DivisionCode, Date]] = set()

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
        capacity_dd = _capacity_by_division_day(panels, settings.rooms, grid, room_dates)
        progressed = False
        for row in short:
            division = row.division
            # Evenings this division is short on, worst deficit first.
            deficits = sorted(
                (
                    (capacity_dd.get((division, day), 0) - demand_dd[(division, day)], day)
                    for (owed_division, day) in demand_dd
                    if owed_division == division
                ),
                key=lambda pair: pair[0],
            )
            short_days = [day for gap, day in deficits if gap < 0] or [day for _g, day in deficits]
            for day in short_days:
                if (division, day) in saturated:
                    continue
                added[division] += 1
                kept, note = _try_add_capacity_panel(
                    panels,
                    settings,
                    grid,
                    room_dates,
                    division,
                    day,
                    f"{division.value}-{AUTO_PANEL_TAG}-{added[division]}",
                )
                if not kept:
                    added[division] -= 1
                    saturated.add((division, day))
                    notes.append(
                        f"{division.value} ({labels.get(day, day.isoformat())}): every room "
                        "open that evening is full — cannot add capacity without another room "
                        "or more time. The solver will produce its best schedule with the "
                        "rooms available."
                    )
                    continue
                progressed = True
                if note:
                    notes.append(note)
                break  # one panel per division per round; then recompute
        if not progressed:
            break

    if not added:
        return settings, notes

    augmented = settings.model_copy(update={"panels": PanelsConfig(panels=panels)})
    messages = [
        f"Auto-scaled {division.value}: added {count} panel(s)"
        for division, count in sorted(added.items(), key=lambda kv: kv[0].value)
    ]
    return augmented, messages + notes


def _event_day_labels(event: EventConfig) -> dict[Date, str]:
    return {day.date: day.label for day in event.days}


def _demand_by_division_day(
    applicants: list[Applicant], grid: SlotGrid
) -> dict[tuple[DivisionCode, Date], int]:
    """(division, day) -> interviews that division is expected to run that
    evening. An applicant tied to one evening lands there; an applicant free
    both evenings is assigned to the currently lighter evening for that
    division, so `Σ_days demand == the division's real demand` rather than
    double-counting wide-availability applicants into a phantom shortfall."""
    date_of_slot = {slot.slot_id: slot.date for slot in grid.slots}
    event_days = sorted({slot.date for slot in grid.slots})
    demand: Counter[tuple[DivisionCode, Date]] = Counter()
    flexible: Counter[DivisionCode] = Counter()

    for applicant in applicants:
        days = {date_of_slot[s] for s in applicant.availability_slots if s in date_of_slot}
        divisions = [applicant.division_1]
        if applicant.division_2 is not None:
            divisions.append(applicant.division_2)
        for division in divisions:
            if len(days) == 1:
                demand[(division, next(iter(days)))] += 1
            elif days:
                flexible[division] += 1

    for division, count in flexible.items():
        for _ in range(count):
            lighter = event_days[0]
            for day in event_days[1:]:
                if demand[(division, day)] < demand[(division, lighter)]:
                    lighter = day
            demand[(division, lighter)] += 1
    return demand


def _capacity_by_division_day(
    panels: list[PanelEntry],
    rooms: RoomsConfig,
    grid: SlotGrid,
    room_dates: dict[str, set[Date]],
) -> dict[tuple[DivisionCode, Date], int]:
    """(division, day) -> interviews that division's panels can run that day.

    A division's panels never share a room (the room-exclusivity rule), so in
    any one slot it runs at most one interview per *distinct room* it has a
    panel active in — a second panel in a room it is already using adds nothing
    and neither does an (n+1)-th panel once it holds one in every room open
    that evening. That is how the load-balancer knows to stop splitting and
    flag a genuine room shortage instead of stacking phantom panels."""
    slot_day = {slot.slot_id: slot.date for slot in grid.slots}

    active_rooms: dict[tuple[DivisionCode, str], set[str]] = defaultdict(set)
    for panel in panels:
        for sid in _panel_active_slot_ids(panel, grid, room_dates.get(panel.room)):
            active_rooms[(panel.division, sid)].add(panel.room)

    capacity: Counter[tuple[DivisionCode, Date]] = Counter()
    for (division, sid), room_ids in active_rooms.items():
        capacity[(division, slot_day[sid])] += len(room_ids)
    return capacity


def _division_day_panel_cap(
    rooms: RoomsConfig,
    division: DivisionCode,
    day: Date,
    room_dates: dict[str, set[Date]],
    event_dates: set[Date],
) -> int:
    """Most panels the proactive load-balancer gives one division on one
    evening (req. 4): one per room open that evening — `_pick_panel_room` fills
    them Room 1, Room 2, ..., Room n, and stops. A genuine shortfall past that
    is `autoscale_panels`' job, where the rare same-room fallback lives."""
    return len(_rooms_for_division_on_day(rooms, division, day, room_dates, event_dates))


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
    each" (SPEC.md §5.5), plus any room fallback note.
    """
    rooms = settings.rooms
    labels = _event_day_labels(settings.event)
    room_dates = _room_dates(rooms, grid)
    event_dates = {d.date for d in settings.event.days}

    demand = _demand_by_division_day(applicants, grid)
    panels: list[PanelEntry] = list(settings.panels.panels)
    added: Counter[DivisionCode] = Counter()
    messages: list[str] = []
    # (division, day) whose rooms are full — another split adds no usable panel.
    saturated: set[tuple[DivisionCode, Date]] = set()

    for _ in range(max_rounds):
        capacity = _capacity_by_division_day(panels, settings.rooms, grid, room_dates)
        counts_dd: Counter[tuple[DivisionCode, Date]] = Counter()
        for panel in panels:
            for day in event_dates:
                if _panel_runs_on_day(panel, day, room_dates, event_dates):
                    counts_dd[(panel.division, day)] += 1

        # Pick the single most-overloaded (division, day) this round, add one
        # panel in a fresh room that evening, then recompute from scratch so
        # the re-spread across every panel is reflected before the next split.
        candidates: list[tuple[float, str, Date, DivisionCode, int]] = []
        for (division, day), need in demand.items():
            if (division, day) in saturated:
                continue
            slot_cap = capacity.get((division, day), 0)
            if slot_cap <= 0 or need <= 0:
                continue
            util = need / slot_cap
            if util <= threshold:
                continue
            if counts_dd[(division, day)] >= _division_day_panel_cap(
                rooms, division, day, room_dates, event_dates
            ):
                continue
            candidates.append((util, division.value, day, division, need))

        if not candidates:
            break
        candidates.sort(key=lambda c: (-c[0], c[1], c[2].isoformat()))
        _util, _name, day, division, need = candidates[0]

        before = counts_dd[(division, day)]
        added[division] += 1
        kept, note = _try_add_capacity_panel(
            panels,
            settings,
            grid,
            room_dates,
            division,
            day,
            f"{division.value}-{BALANCE_PANEL_TAG}-{added[division]}",
        )
        if not kept:
            added[division] -= 1
            saturated.add((division, day))
            messages.append(
                f"{division.value} ({labels.get(day, day.isoformat())}) is at room capacity "
                "for that evening — no further split adds a usable panel; add a room to grow it."
            )
            continue
        if note:
            messages.append(note)
        messages.append(
            f"Rebalanced {division.value} ({labels.get(day, day.isoformat())}): "
            f"{before}→{before + 1} panels, ~{round(need / (before + 1))} applicants each"
        )

    if not added:
        return settings, messages

    augmented = settings.model_copy(update={"panels": PanelsConfig(panels=panels)})
    return augmented, messages
