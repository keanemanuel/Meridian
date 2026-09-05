"""Tests for M5 review — locks and the edit validator (SPEC.md §3.5 FR-40..FR-45,
§11 manual adjustment model; CLAUDE.md invariant 4: "Manual edits are sacred").

Two things must hold:

1. A locked assignment survives a re-solve unchanged (FR-41, C6).
2. An illegal manual edit — double-booked applicant, panel busy, room over
   capacity, or a slot outside the grid — is rejected with a specific
   message and never silently turned into a lock (FR-42, E-12).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, time

import pytest

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.domain.models import Applicant, Assignment, Panel, Room, Slot
from iff_scheduler.review.edit_validator import validate_edits
from iff_scheduler.review.locks import (
    lock_from_assignment,
    lock_to_row,
    merge_locks,
    parse_lock_rows,
)
from iff_scheduler.scheduling.base import Lock, SolveProblem
from iff_scheduler.scheduling.solver_cpsat import CpSatSolver
from iff_scheduler.settings import DayConfig, EventConfig, SolverWeights

DAY = date(2026, 9, 17)
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


def make_assignment(
    applicant_id: str,
    choice_index: int,
    division: DivisionCode,
    panel: Panel,
    slot: Slot,
    *,
    sub_division: str = "Sub",
    is_clash: bool = False,
    is_locked: bool = False,
) -> Assignment:
    return Assignment(
        applicant_id=applicant_id,
        full_name=f"Applicant {applicant_id}",
        email=f"{applicant_id.lower()}@example.com",
        choice_index=1 if choice_index == 1 else 2,
        sub_division=sub_division,
        division=division,
        panel_id=panel.id,
        room=panel.room,
        slot_id=slot.slot_id,
        date=slot.date,
        start_time=slot.start_time,
        end_time=slot.end_time,
        is_clash=is_clash,
        is_locked=is_locked,
        same_parent_pair=False,
        reason=None,
    )


# ------------------------------------------------------------- locks.py


def test_lock_from_assignment_pins_panel_and_slot() -> None:
    slots = make_slots(2)
    panel = make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots)
    assignment = make_assignment("A1", 1, DivisionCode.CREATIVE, panel, slots[0])

    lock = lock_from_assignment(assignment)

    assert lock == Lock(
        applicant_id="A1", choice_index=1, panel_id="CREATIVE-A", slot_id=slots[0].slot_id
    )


def test_parse_lock_rows_round_trips_with_lock_to_row() -> None:
    lock = Lock(applicant_id="A2", choice_index=2, panel_id="LOGISTICS-A", slot_id="S1")

    parsed = parse_lock_rows([lock_to_row(lock)])

    assert parsed == [lock]


def test_parse_lock_rows_rejects_missing_column() -> None:
    with pytest.raises(ValueError, match="missing column"):
        parse_lock_rows([{"applicant_id": "A1", "choice_index": "1", "panel_id": "P1"}])


def test_parse_lock_rows_rejects_non_integer_choice_index() -> None:
    with pytest.raises(ValueError, match="integer"):
        parse_lock_rows(
            [{"applicant_id": "A1", "choice_index": "one", "panel_id": "P1", "slot_id": "S1"}]
        )


def test_parse_lock_rows_rejects_out_of_range_choice_index() -> None:
    with pytest.raises(ValueError, match="1 or 2"):
        parse_lock_rows(
            [{"applicant_id": "A1", "choice_index": "3", "panel_id": "P1", "slot_id": "S1"}]
        )


def test_merge_locks_is_cumulative() -> None:
    """SPEC.md §11: "Locks are cumulative and explicit" — an existing pin for a
    different choice survives a merge that only touches one choice."""
    existing = [
        Lock(applicant_id="A1", choice_index=1, panel_id="CREATIVE-A", slot_id="S1"),
        Lock(applicant_id="A2", choice_index=1, panel_id="CREATIVE-A", slot_id="S2"),
    ]
    incoming = [Lock(applicant_id="A3", choice_index=2, panel_id="LOGISTICS-A", slot_id="S3")]

    merged = merge_locks(existing, incoming)

    assert len(merged) == 3
    assert Lock(applicant_id="A1", choice_index=1, panel_id="CREATIVE-A", slot_id="S1") in merged
    assert Lock(applicant_id="A2", choice_index=1, panel_id="CREATIVE-A", slot_id="S2") in merged
    assert Lock(applicant_id="A3", choice_index=2, panel_id="LOGISTICS-A", slot_id="S3") in merged


def test_merge_locks_re_locking_a_choice_replaces_its_old_pin() -> None:
    """A recruiter moves an already-locked interview and re-locks it: the stale
    pin is replaced, not duplicated (SPEC.md §11)."""
    existing = [Lock(applicant_id="A1", choice_index=1, panel_id="CREATIVE-A", slot_id="S1")]
    incoming = [Lock(applicant_id="A1", choice_index=1, panel_id="CREATIVE-B", slot_id="S4")]

    merged = merge_locks(existing, incoming)

    assert merged == [Lock(applicant_id="A1", choice_index=1, panel_id="CREATIVE-B", slot_id="S4")]


# ------------------------------------------------------- edit_validator.py


def test_validate_edits_accepts_a_legal_schedule() -> None:
    slots = make_slots(4)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    creative = make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots)
    logistics = make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots)
    panels = [creative, logistics]
    assignments = [
        make_assignment("A1", 1, DivisionCode.CREATIVE, creative, slots[0]),
        make_assignment("A1", 2, DivisionCode.LOGISTICS, logistics, slots[1]),
    ]

    assert validate_edits(assignments, panels, rooms, slots) == []


def test_validate_edits_rejects_double_booked_applicant() -> None:
    """E-12: the recruiter moved choice 2 onto the same slot as choice 1."""
    slots = make_slots(4)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS])]
    creative = make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots)
    logistics = make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots)
    panels = [creative, logistics]
    assignments = [
        make_assignment("A1", 1, DivisionCode.CREATIVE, creative, slots[0]),
        make_assignment("A1", 2, DivisionCode.LOGISTICS, logistics, slots[0]),
    ]

    violations = validate_edits(assignments, panels, rooms, slots)

    codes = {v.code for v in violations}
    assert "DOUBLE_BOOKED_APPLICANT" in codes
    hit = next(v for v in violations if v.code == "DOUBLE_BOOKED_APPLICANT")
    assert hit.applicant_id == "A1"
    assert slots[0].slot_id in hit.message


def test_validate_edits_rejects_panel_busy() -> None:
    """E-12: two different applicants edited onto the same panel and slot."""
    slots = make_slots(2)
    rooms = [make_room("R1", [DivisionCode.CREATIVE])]
    creative = make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots)
    panels = [creative]
    assignments = [
        make_assignment("A1", 1, DivisionCode.CREATIVE, creative, slots[0]),
        make_assignment("A2", 1, DivisionCode.CREATIVE, creative, slots[0]),
    ]

    violations = validate_edits(assignments, panels, rooms, slots)

    codes = {v.code for v in violations}
    assert "PANEL_BUSY" in codes
    hit = next(v for v in violations if v.code == "PANEL_BUSY")
    assert "CREATIVE-A" in hit.message
    assert "A1" in hit.message and "A2" in hit.message


def test_validate_edits_rejects_room_over_capacity() -> None:
    """E-12: room R1 admits one interview at a time, but the edit puts two
    different panels in it during the same slot."""
    slots = make_slots(2)
    rooms = [make_room("R1", [DivisionCode.CREATIVE, DivisionCode.LOGISTICS], capacity=1)]
    creative = make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots)
    logistics = make_panel("LOGISTICS-A", DivisionCode.LOGISTICS, "R1", slots)
    panels = [creative, logistics]
    assignments = [
        make_assignment("A1", 1, DivisionCode.CREATIVE, creative, slots[0]),
        make_assignment("A2", 1, DivisionCode.LOGISTICS, logistics, slots[0]),
    ]

    violations = validate_edits(assignments, panels, rooms, slots)

    codes = {v.code for v in violations}
    assert "ROOM_OVER_CAPACITY" in codes
    hit = next(v for v in violations if v.code == "ROOM_OVER_CAPACITY")
    assert "R1" in hit.message
    assert "1" in hit.message  # max_concurrent_panels quoted in the message


def test_validate_edits_rejects_slot_outside_grid() -> None:
    """A recruiter typo'd a slot id that isn't on the event grid at all."""
    slots = make_slots(2)
    rooms = [make_room("R1", [DivisionCode.CREATIVE])]
    creative = make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots)
    panels = [creative]
    bogus = slots[0].model_copy(update={"slot_id": "2099-01-01_0000"})
    assignments = [make_assignment("A1", 1, DivisionCode.CREATIVE, creative, bogus)]

    violations = validate_edits(assignments, panels, rooms, slots)

    assert len(violations) == 1
    assert violations[0].code == "SLOT_OUTSIDE_GRID"
    assert violations[0].applicant_id == "A1"


def test_validate_edits_rejects_unknown_panel() -> None:
    slots = make_slots(2)
    rooms = [make_room("R1", [DivisionCode.CREATIVE])]
    real = make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots)
    ghost = make_panel("CREATIVE-GHOST", DivisionCode.CREATIVE, "R1", slots)
    assignments = [make_assignment("A1", 1, DivisionCode.CREATIVE, ghost, slots[0])]

    violations = validate_edits(assignments, [real], rooms, slots)

    assert len(violations) == 1
    assert violations[0].code == "UNKNOWN_PANEL"


def test_validate_edits_rejects_panel_inactive_at_that_slot() -> None:
    """C7: the recruiter placed an interview outside the panel's active window."""
    slots = make_slots(4)
    rooms = [make_room("R1", [DivisionCode.CREATIVE])]
    limited = make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots, active=slots[:1])
    assignments = [make_assignment("A1", 1, DivisionCode.CREATIVE, limited, slots[3])]

    violations = validate_edits(assignments, [limited], rooms, slots)

    codes = {v.code for v in violations}
    assert "PANEL_INACTIVE" in codes


def test_validate_edits_rejects_division_mismatch() -> None:
    """The recruiter moved a LOGISTICS choice onto a CREATIVE panel."""
    slots = make_slots(2)
    rooms = [make_room("R1", [DivisionCode.CREATIVE])]
    creative = make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots)
    assignments = [make_assignment("A1", 2, DivisionCode.LOGISTICS, creative, slots[0])]

    violations = validate_edits(assignments, [creative], rooms, slots)

    codes = {v.code for v in violations}
    assert "DIVISION_MISMATCH" in codes


def test_validate_edits_reports_every_violation_not_just_the_first() -> None:
    """FR-42: the recruiter should see all the reasons an edit was rejected in
    one pass, not fix one and hit the next on a second attempt."""
    slots = make_slots(2)
    rooms = [make_room("R1", [DivisionCode.CREATIVE], capacity=1)]
    creative = make_panel("CREATIVE-A", DivisionCode.CREATIVE, "R1", slots)
    panels = [creative]
    assignments = [
        make_assignment("A1", 1, DivisionCode.CREATIVE, creative, slots[0]),
        make_assignment("A1", 2, DivisionCode.CREATIVE, creative, slots[0]),
        make_assignment("A2", 1, DivisionCode.CREATIVE, creative, slots[0]),
    ]

    violations = validate_edits(assignments, panels, rooms, slots)

    codes = {v.code for v in violations}
    assert "DOUBLE_BOOKED_APPLICANT" in codes
    assert "PANEL_BUSY" in codes


# -------------------------------------------- end-to-end: locks survive a re-solve


def test_a_locked_assignment_survives_a_re_solve_unchanged() -> None:
    """CLAUDE.md invariant 4 / FR-41: solve once, lock the result, solve again
    with more applicants added — the locked interview must not move, even
    though the solver is free to place everything else."""
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

    problem_1 = SolveProblem(
        applicants=applicants,
        panels=panels,
        rooms=rooms,
        slots=slots,
        weights=WEIGHTS,
        time_limit_seconds=20.0,
    )
    result_1 = CpSatSolver().solve(problem_1)
    assert result_1.assignments

    pinned = next(a for a in result_1.assignments if a.applicant_id == "A0")
    lock = lock_from_assignment(pinned)

    late_arrival = make_applicant("A_LATE", DivisionCode.CREATIVE, DivisionCode.LOGISTICS, slots)
    problem_2 = SolveProblem(
        applicants=[*applicants, late_arrival],
        panels=panels,
        rooms=rooms,
        slots=slots,
        weights=WEIGHTS,
        locks=[lock],
        time_limit_seconds=20.0,
    )
    result_2 = CpSatSolver().solve(problem_2)

    re_solved = next(
        a
        for a in result_2.assignments
        if a.applicant_id == pinned.applicant_id and a.choice_index == pinned.choice_index
    )
    assert re_solved.panel_id == pinned.panel_id
    assert re_solved.slot_id == pinned.slot_id
    assert re_solved.is_locked is True
