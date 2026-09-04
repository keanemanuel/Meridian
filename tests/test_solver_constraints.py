"""Constraint tests for the CP-SAT scheduler (SPEC.md §5.2, C1-C8).

Every hard constraint gets both a positive test (a solution that satisfies it)
and, where the constraint can bind, a negative test that constructs a scenario
which can only be solved by violating it and asserts the solver refuses rather
than quietly producing an illegal schedule (CLAUDE.md, "Conventions").

The decision variable is x[applicant, choice_index, panel, slot] — indexed by
*choice*, never by division, so an applicant whose two sub-divisions share a
parent division still gets two separate interviews (SPEC.md §1.2 Finding B).
"""

from __future__ import annotations

import time as timer
from collections.abc import Iterable, Sequence
from datetime import date, datetime, time

import pytest

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.domain.models import Applicant, Assignment, ChoiceIndex, Panel, Room, Slot
from iff_scheduler.scheduling.base import (
    USABLE_STATUSES,
    Lock,
    SolveProblem,
    resolve_panels,
    resolve_rooms,
)
from iff_scheduler.scheduling.objectives import score_schedule
from iff_scheduler.scheduling.solver_cpsat import CpSatSolver
from iff_scheduler.settings import DayConfig, EventConfig, SolverWeights, load_settings

DAY = date(2026, 9, 17)
DAY_2 = date(2026, 9, 18)

WEIGHTS = SolverWeights(clash=10_000, repeat_panel=50, spread=10, balance=5, lateness=1)


# ---------------------------------------------------------------- builders


def make_slots(count: int, day: date = DAY) -> list[Slot]:
    """`count` 20-minute slots from 18:00 on `day`."""
    minutes = 20 * count
    event = EventConfig(
        event_name="Test",
        timezone="Asia/Jakarta",
        interview_duration_minutes=20,
        days=[
            DayConfig(
                date=day,
                label="Thu",
                start=time(18, 0),
                end=time(18 + minutes // 60, minutes % 60),
            )
        ],
    )
    return build_slot_grid(event).slots


def make_panel(
    panel_id: str,
    division: DivisionCode,
    room: str,
    slots: Sequence[Slot],
    active: Iterable[Slot] | None = None,
) -> Panel:
    active_slots = slots if active is None else active
    return Panel(
        id=panel_id,
        division=division,
        room=room,
        active_slot_ids=[s.slot_id for s in active_slots],
    )


def make_room(room_id: str, divisions: Sequence[DivisionCode], capacity: int = 99) -> Room:
    return Room(id=room_id, max_concurrent_panels=capacity, divisions=list(divisions))


def make_applicant(
    applicant_id: str,
    division_1: DivisionCode,
    division_2: DivisionCode,
    available: Sequence[Slot],
    sub_division_1: str = "Sub One",
    sub_division_2: str = "Sub Two",
) -> Applicant:
    return Applicant(
        applicant_id=applicant_id,
        full_name=f"Applicant {applicant_id}",
        email=f"{applicant_id.lower()}@example.com",
        phone="",
        sub_division_1=sub_division_1,
        sub_division_2=sub_division_2,
        division_1=division_1,
        division_2=division_2,
        availability_slots=[s.slot_id for s in available],
        submitted_at=datetime(2026, 8, 1, 9, 0),
        notes=None,
    )


def make_problem(
    applicants: Sequence[Applicant],
    panels: Sequence[Panel],
    rooms: Sequence[Room],
    slots: Sequence[Slot],
    *,
    min_gap_slots: int = 0,
    locks: Sequence[Lock] = (),
    two_phase: bool = True,
    time_limit_seconds: float = 20.0,
) -> SolveProblem:
    return SolveProblem(
        applicants=list(applicants),
        panels=list(panels),
        rooms=list(rooms),
        slots=list(slots),
        weights=WEIGHTS,
        min_gap_slots=min_gap_slots,
        locks=list(locks),
        two_phase=two_phase,
        time_limit_seconds=time_limit_seconds,
    )


def solve(problem: SolveProblem) -> object:
    return CpSatSolver().solve(problem)


def by_choice(assignments: Sequence[Assignment]) -> dict[tuple[str, ChoiceIndex], Assignment]:
    return {(a.applicant_id, a.choice_index): a for a in assignments}


def slot_index_of(slots: Sequence[Slot], slot_id: str) -> int:
    return next(s.slot_index for s in slots if s.slot_id == slot_id)


# ------------------------------------------------------ C1  completeness


def test_c1_every_choice_gets_exactly_one_interview() -> None:
    """C1 / FR-30: for every (applicant, choice), sum over panels and slots == 1."""
    slots = make_slots(4)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicants = [
        make_applicant(f"A{i}", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)
        for i in range(3)
    ]

    result = solve(make_problem(applicants, panels, rooms, slots))

    assert result.status in USABLE_STATUSES
    assert len(result.assignments) == 6
    placed = by_choice(result.assignments)
    assert len(placed) == 6
    for applicant in applicants:
        assert (applicant.applicant_id, 1) in placed
        assert (applicant.applicant_id, 2) in placed


def test_c1_same_parent_pair_still_gets_two_interviews() -> None:
    """C1 / FR-04b / E-01: Creative + WebMaster both map to CREATIVE. Indexing by
    choice rather than division is what keeps this two interviews, not one."""
    slots = make_slots(4)
    rooms = [make_room("R1", [DivisionCode.CREATIVE])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("CREATIVE-B", DivisionCode.CREATIVE, "R1", slots),
    ]
    applicant = make_applicant(
        "A1",
        DivisionCode.CREATIVE,
        DivisionCode.CREATIVE,
        slots,
        sub_division_1="Creative",
        sub_division_2="WebMaster",
    )

    result = solve(make_problem([applicant], panels, rooms, slots))

    assert result.status in USABLE_STATUSES
    assert len(result.assignments) == 2
    assert {a.choice_index for a in result.assignments} == {1, 2}
    assert {a.sub_division for a in result.assignments} == {"Creative", "WebMaster"}
    assert all(a.division is DivisionCode.CREATIVE for a in result.assignments)
    assert all(a.same_parent_pair for a in result.assignments)


def test_c1_refuses_when_a_choice_has_no_panel_of_its_division() -> None:
    """C1 is never traded away: with no LOGISTICS panel the instance is infeasible,
    and the solver must say so rather than return a one-interview schedule."""
    slots = make_slots(4)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots)]
    applicant = make_applicant("A1", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)

    result = solve(make_problem([applicant], panels, rooms, slots))

    assert result.status == "INFEASIBLE"
    assert result.assignments == []


# -------------------------------------------------- C2  panel exclusivity


def test_c2_no_panel_is_double_booked_in_a_slot() -> None:
    """C2 / FR-23: each panel conducts at most one interview per slot."""
    slots = make_slots(4)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicants = [
        make_applicant(f"A{i}", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)
        for i in range(4)
    ]

    result = solve(make_problem(applicants, panels, rooms, slots))

    assert result.status in USABLE_STATUSES
    seen: set[tuple[str, str]] = set()
    for assignment in result.assignments:
        key = (assignment.panel_id, assignment.slot_id)
        assert key not in seen, f"panel {key[0]} double-booked at {key[1]}"
        seen.add(key)


def test_c2_refuses_when_demand_exceeds_panel_slot_capacity() -> None:
    """Three CREATIVE interviews, one CREATIVE panel, two slots: the only way to
    place them all is to double-book the panel, so the solver must refuse."""
    slots = make_slots(2)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicants = [
        make_applicant("A1", DivisionCode.CREATIVE, DivisionCode.CREATIVE, slots),
        make_applicant("A2", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots),
    ]

    result = solve(make_problem(applicants, panels, rooms, slots))

    assert result.status == "INFEASIBLE"
    assert result.assignments == []


# ---------------------------------------------- C3  applicant exclusivity


def test_c3_an_applicants_two_interviews_never_share_a_slot() -> None:
    """C3 / FR-31: no applicant is in two places at once."""
    slots = make_slots(4)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicants = [
        make_applicant(f"A{i}", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)
        for i in range(3)
    ]

    result = solve(make_problem(applicants, panels, rooms, slots))

    assert result.status in USABLE_STATUSES
    for applicant in applicants:
        theirs = [a for a in result.assignments if a.applicant_id == applicant.applicant_id]
        assert len({a.slot_id for a in theirs}) == 2


def test_c3_refuses_when_only_one_slot_exists() -> None:
    """One slot on the grid means both interviews would collide, so even though
    panel and room capacity are ample the instance is infeasible."""
    slots = make_slots(1)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicant = make_applicant("A1", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)

    result = solve(make_problem([applicant], panels, rooms, slots))

    assert result.status == "INFEASIBLE"
    assert result.assignments == []


# ------------------------------------------------- C4  room concurrency


def test_c4_room_concurrency_is_never_exceeded() -> None:
    """C4 / FR-24: a room hosts at most `max_concurrent_panels` interviews per slot."""
    slots = make_slots(6)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS], capacity=1)]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicants = [
        make_applicant(f"A{i}", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)
        for i in range(3)
    ]

    result = solve(make_problem(applicants, panels, rooms, slots))

    assert result.status in USABLE_STATUSES
    per_slot: dict[str, int] = {}
    for assignment in result.assignments:
        per_slot[assignment.slot_id] = per_slot.get(assignment.slot_id, 0) + 1
    assert per_slot and max(per_slot.values()) == 1


def test_c4_refuses_when_room_capacity_cannot_hold_the_demand() -> None:
    """Four interviews, two slots, one room that admits one interview at a time:
    capacity is 2, demand is 4. The solver must refuse rather than overfill."""
    slots = make_slots(2)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS], capacity=1)]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("CREATIVE-B", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
        make_panel("LOGISTICS-B", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicants = [
        make_applicant(f"A{i}", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)
        for i in range(2)
    ]

    result = solve(make_problem(applicants, panels, rooms, slots))

    assert result.status == "INFEASIBLE"
    assert result.assignments == []


# ------------------------------------------------------ C5  minimum gap


def test_c5_min_gap_slots_separates_an_applicants_two_interviews() -> None:
    """C5 / FR-32: `min_gap_slots: 1` leaves at least one free slot between an
    applicant's two interviews, so a cross-room pair has travel time (E-07)."""
    slots = make_slots(6)
    rooms = [make_room("R1", [DivisionCode.CREATIVE]), make_room("R2", [DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R2", slots),
    ]
    applicants = [
        make_applicant(f"A{i}", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)
        for i in range(2)
    ]

    result = solve(make_problem(applicants, panels, rooms, slots, min_gap_slots=1))

    assert result.status in USABLE_STATUSES
    for applicant in applicants:
        theirs = [a for a in result.assignments if a.applicant_id == applicant.applicant_id]
        indices = sorted(slot_index_of(slots, a.slot_id) for a in theirs)
        assert indices[1] - indices[0] >= 2


def test_c5_refuses_when_the_gap_cannot_be_honoured() -> None:
    """Two adjacent slots and `min_gap_slots: 1`: back-to-back is the only
    placement, and it is forbidden, so the solver must refuse."""
    slots = make_slots(2)
    rooms = [make_room("R1", [DivisionCode.CREATIVE]), make_room("R2", [DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R2", slots),
    ]
    applicant = make_applicant("A1", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)

    result = solve(make_problem([applicant], panels, rooms, slots, min_gap_slots=1))

    assert result.status == "INFEASIBLE"
    assert result.assignments == []


# ------------------------------------------------------------ C6  locks


def test_c6_a_locked_assignment_is_reproduced_exactly() -> None:
    """C6 / FR-41: the solver schedules around a human decision, never over it."""
    slots = make_slots(6)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicants = [
        make_applicant(f"A{i}", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)
        for i in range(3)
    ]
    lock = Lock(applicant_id="A2", choice_index=2, panel_id="LOGISTICS-A", slot_id=slots[5].slot_id)

    result = solve(make_problem(applicants, panels, rooms, slots, locks=[lock]))

    assert result.status in USABLE_STATUSES
    locked = by_choice(result.assignments)[("A2", 2)]
    assert locked.panel_id == "LOGISTICS-A"
    assert locked.slot_id == slots[5].slot_id
    assert locked.is_locked is True
    assert all(a.is_locked is False for a in result.assignments if a is not locked)


def test_c6_a_lock_outranks_the_objective_and_forces_a_clash() -> None:
    """A lock is a hard constraint even when it puts the applicant outside their
    declared availability — the clash is reported, not resolved by moving them."""
    slots = make_slots(6)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicant = make_applicant("A1", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots[:3])
    lock = Lock(applicant_id="A1", choice_index=1, panel_id="CREATIVE-A", slot_id=slots[5].slot_id)

    result = solve(make_problem([applicant], panels, rooms, slots, locks=[lock]))

    assert result.status in USABLE_STATUSES
    locked = by_choice(result.assignments)[("A1", 1)]
    assert locked.slot_id == slots[5].slot_id
    assert locked.is_clash is True
    assert result.clash_count == 1


# ---------------------------------------------- C7  panel active windows


def test_c7_a_panel_is_never_used_outside_its_active_window() -> None:
    """C7 / FR-25 / E-09: a panel that is only available for part of the event
    gets no interviews outside that window."""
    slots = make_slots(6)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    limited = make_panel("CREATIVE-B", DivisionCode.CREATIVE, "R1", slots, active=slots[:2])
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        limited,
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicants = [
        make_applicant(f"A{i}", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)
        for i in range(4)
    ]

    result = solve(make_problem(applicants, panels, rooms, slots))

    assert result.status in USABLE_STATUSES
    window = set(limited.active_slot_ids)
    used = [a.slot_id for a in result.assignments if a.panel_id == "CREATIVE-B"]
    assert all(slot_id in window for slot_id in used)


def test_c7_refuses_when_the_active_window_is_too_narrow() -> None:
    """The only CREATIVE panel sits for one slot but two CREATIVE interviews are
    required, so honouring C7 and C1 together is impossible."""
    slots = make_slots(4)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots, active=slots[:1]),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicants = [
        make_applicant(f"A{i}", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)
        for i in range(2)
    ]

    result = solve(make_problem(applicants, panels, rooms, slots))

    assert result.status == "INFEASIBLE"
    assert result.assignments == []


# ------------------------------- C8  distinct panels for same-parent pairs


def test_c8_same_parent_pair_is_given_two_different_panels() -> None:
    """C8 / FR-30b: when the shared division has two panels, the applicant's two
    interviews are conducted by different panels."""
    slots = make_slots(6)
    rooms = [make_room("R1", [DivisionCode.MEDMARDOC])]
    panels = [
        make_panel("MEDMARDOC-A", DivisionCode.MEDMARDOC, "R1", slots),
        make_panel("MEDMARDOC-B", DivisionCode.MEDMARDOC, "R1", slots),
    ]
    applicant = make_applicant(
        "A1",
        DivisionCode.MEDMARDOC,
        DivisionCode.MEDMARDOC,
        slots,
        sub_division_1="Media Marketing",
        sub_division_2="Media Documentation",
    )

    result = solve(make_problem([applicant], panels, rooms, slots))

    assert result.status in USABLE_STATUSES
    assert len({a.panel_id for a in result.assignments}) == 2


def test_c8_auto_relaxes_when_the_division_has_only_one_panel() -> None:
    """E-01c: with a single panel the preference is dropped rather than made
    infeasible — the applicant sees the same panel twice, at different times."""
    slots = make_slots(6)
    rooms = [make_room("R1", [DivisionCode.MEDMARDOC])]
    panels = [make_panel("MEDMARDOC-A", DivisionCode.MEDMARDOC, "R1", slots)]
    applicant = make_applicant(
        "A1",
        DivisionCode.MEDMARDOC,
        DivisionCode.MEDMARDOC,
        slots,
        sub_division_1="Media Marketing",
        sub_division_2="Media Documentation",
    )

    result = solve(make_problem([applicant], panels, rooms, slots))

    assert result.status in USABLE_STATUSES
    assert len(result.assignments) == 2
    assert len({a.panel_id for a in result.assignments}) == 1
    assert len({a.slot_id for a in result.assignments}) == 2


# --------------------------------------------- objective and scale (FR-33..39)


def test_availability_is_preferred_over_earliness() -> None:
    """FR-33: time first. The lateness term prefers slot 0, but the clash weight
    dominates, so an applicant free only late is scheduled late without a clash."""
    slots = make_slots(6)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicant = make_applicant("A1", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots[4:])

    result = solve(make_problem([applicant], panels, rooms, slots))

    assert result.status in USABLE_STATUSES
    assert result.clash_count == 0
    assert {a.slot_id for a in result.assignments} == {s.slot_id for s in slots[4:]}


def test_a_clash_is_forced_and_explained_when_availability_is_too_thin() -> None:
    """FR-34 / E-03: an applicant who ticks one block still gets both interviews;
    the second is placed outside availability and flagged with a reason."""
    slots = make_slots(6)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicant = make_applicant("A1", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots[:1])

    result = solve(make_problem([applicant], panels, rooms, slots))

    assert result.status in USABLE_STATUSES
    assert len(result.assignments) == 2
    clashes = [a for a in result.assignments if a.is_clash]
    assert len(clashes) == 1
    assert result.clash_count == 1
    assert clashes[0].reason


def test_solver_is_deterministic() -> None:
    """FR-35: same inputs, same config, same seed -> byte-identical schedule."""
    slots = make_slots(8)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("CREATIVE-B", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicants = [
        make_applicant(f"A{i}", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)
        for i in range(6)
    ]
    problem = make_problem(applicants, panels, rooms, slots)

    first = solve(problem)
    second = solve(problem)

    assert first.objective_value == second.objective_value
    assert first.assignments == second.assignments


# Demand profile for the full-scale instance, shaped to the panel counts in
# config/panels.yaml (MEDMARDOC 2, CREATIVE 2, LOGISTICS 2, FNB 2, PROGRAM 3,
# LIAISON 2 -> 312 panel-slots for 240 interviews). Per-division demand stays
# under that division's panel x slot supply, which is what the Capacity Advisor
# checks before a solve is ever attempted (SPEC.md §5.5).
_DEMAND_PROFILE: list[tuple[int, DivisionCode, DivisionCode, str, str]] = [
    # 12 same-parent pairs (E-01): both choices under one parent division.
    (6, DivisionCode.CREATIVE, DivisionCode.CREATIVE, "Creative", "WebMaster"),
    (6, DivisionCode.MEDMARDOC, DivisionCode.MEDMARDOC, "Media Marketing", "Media Documentation"),
    (32, DivisionCode.CREATIVE, DivisionCode.LOGISTICS, "Creative", "Logistics"),
    (32, DivisionCode.MEDMARDOC, DivisionCode.LIAISON, "Media Marketing", "Liaison"),
    (32, DivisionCode.FNB, DivisionCode.PROGRAM, "Finance & Booth", "Program"),
    (4, DivisionCode.LOGISTICS, DivisionCode.LIAISON, "Logistics", "Liaison"),
    (4, DivisionCode.LIAISON, DivisionCode.FNB, "Liaison", "Finance & Booth"),
    (4, DivisionCode.FNB, DivisionCode.LOGISTICS, "Finance & Booth", "Logistics"),
]


def _full_scale_problem() -> SolveProblem:
    """120 applicants x 2 choices = 240 interviews, against the committed config."""
    settings = load_settings()
    grid = build_slot_grid(settings.event)
    slots = grid.slots
    assert len(slots) == 24  # 2 days x 4 hours / 20 minutes

    applicants: list[Applicant] = []
    for count, division_1, division_2, sub_1, sub_2 in _DEMAND_PROFILE:
        for _ in range(count):
            index = len(applicants)
            # Availability: a 16-slot window per applicant, offset so the load is
            # spread rather than everyone ticking the same blocks.
            start = (index * 5) % (len(slots) - 15)
            applicants.append(
                make_applicant(
                    f"A{index:03d}",
                    division_1,
                    division_2,
                    slots[start : start + 16],
                    sub_division_1=sub_1,
                    sub_division_2=sub_2,
                )
            )
    assert len(applicants) == 120

    return SolveProblem(
        applicants=applicants,
        panels=resolve_panels(settings.panels, grid),
        rooms=resolve_rooms(settings.rooms),
        slots=slots,
        weights=settings.solver.weights,
        min_gap_slots=settings.event.min_gap_slots,
        two_phase=settings.solver.two_phase,
        time_limit_seconds=float(settings.solver.time_limit_seconds),
        phase1_time_fraction=settings.solver.phase1_time_fraction,
        random_seed=settings.solver.random_seed,
    )


@pytest.mark.slow
def test_full_scale_240_interviews_solve_within_the_time_budget() -> None:
    """M3 definition of done / FR-39: 240 interviews placed in under 60 seconds."""
    problem = _full_scale_problem()

    started = timer.perf_counter()
    result = solve(problem)
    elapsed = timer.perf_counter() - started

    assert result.status in USABLE_STATUSES
    assert len(result.assignments) == 240
    assert len(by_choice(result.assignments)) == 240
    assert elapsed < 60.0

    # C2 and C3 hold at scale too.
    panel_slots = {(a.panel_id, a.slot_id) for a in result.assignments}
    assert len(panel_slots) == 240
    applicant_slots = {(a.applicant_id, a.slot_id) for a in result.assignments}
    assert len(applicant_slots) == 240


def test_cpsat_objective_matches_the_independent_score() -> None:
    """`objectives.score_schedule` is what `metrics.json` reports and what the
    greedy fallback (SPEC.md §5.3) will be compared on. It must agree with the
    expression CP-SAT actually minimised, or the two solvers are not comparable."""
    slots = make_slots(8)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    panels = [
        make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots),
        make_panel("CREATIVE-B", DivisionCode.CREATIVE, "R1", slots),
        make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots),
    ]
    applicants = [
        make_applicant("A0", DivisionCode.CREATIVE, DivisionCode.CREATIVE, slots),
        make_applicant("A1", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots[:2]),
        make_applicant("A2", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots),
        make_applicant("A3", DivisionCode.LOGISTICS, DivisionCode.CREATIVE, slots[3:]),
    ]
    problem = make_problem(applicants, panels, rooms, slots)

    result = solve(problem)

    assert result.status in USABLE_STATUSES
    breakdown = score_schedule(result.assignments, applicants, panels, slots, WEIGHTS)
    assert breakdown.total == result.objective_value
    assert breakdown.clashes == result.clash_count
