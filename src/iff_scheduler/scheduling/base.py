"""Solver protocol and the problem/result types every solver speaks.

Both the CP-SAT solver and the greedy fallback (SPEC.md §5.3) implement
`Solver.solve(problem) -> SolveResult`, so they are interchangeable and
comparable. Pure: no I/O, no adapters, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from iff_scheduler.domain.grid import SlotGrid
from iff_scheduler.domain.models import Applicant, Assignment, ChoiceIndex, Panel, Room, Slot
from iff_scheduler.settings import PanelsConfig, RoomsConfig, SolverWeights

# CP-SAT status names we treat as "a schedule came back".
USABLE_STATUSES = frozenset({"OPTIMAL", "FEASIBLE"})


@dataclass(frozen=True)
class Lock:
    """A manual decision the solver must treat as fixed (C6, FR-41).

    Pins one applicant *choice* — not one applicant — to a panel and slot, so
    a same-parent pair can have one interview locked and the other free.
    """

    applicant_id: str
    choice_index: ChoiceIndex
    panel_id: str
    slot_id: str


@dataclass(frozen=True)
class SolveProblem:
    """Everything a solver needs, already resolved from config to domain objects."""

    applicants: list[Applicant]
    panels: list[Panel]
    rooms: list[Room]
    slots: list[Slot]
    weights: SolverWeights
    min_gap_slots: int = 0
    locks: list[Lock] = field(default_factory=list)
    two_phase: bool = True
    time_limit_seconds: float = 60.0
    phase1_time_fraction: float = 0.5
    random_seed: int = 42


@dataclass(frozen=True)
class SolveResult:
    """One solve run's output. `status == "INFEASIBLE"` means no schedule exists
    under the given hard constraints — assignments is then empty and the caller
    is expected to report which constraint is binding (E-18)."""

    assignments: list[Assignment]
    status: str
    objective_value: int
    clash_count: int
    solve_seconds: float
    phase: int
    log: list[str] = field(default_factory=list)


class Solver(Protocol):
    def solve(self, problem: SolveProblem) -> SolveResult: ...


def resolve_panels(panels: PanelsConfig, grid: SlotGrid) -> list[Panel]:
    """Turn configured panels into domain panels with their active slots resolved.

    A panel that declares no `active_windows` is active for the whole event
    (FR-25). Slot ids come back in grid order, so downstream iteration is
    deterministic (FR-35).
    """
    resolved: list[Panel] = []
    for entry in panels.panels:
        if entry.active_windows:
            active = [
                slot.slot_id
                for slot in grid.slots
                if any(
                    window.date == slot.date
                    and window.start <= slot.start_time
                    and slot.end_time <= window.end
                    for window in entry.active_windows
                )
            ]
        else:
            active = [slot.slot_id for slot in grid.slots]
        resolved.append(
            Panel(id=entry.id, division=entry.division, room=entry.room, active_slot_ids=active)
        )
    return resolved


def resolve_rooms(rooms: RoomsConfig) -> list[Room]:
    return [
        Room(id=r.id, max_concurrent_panels=r.max_concurrent_panels, divisions=list(r.divisions))
        for r in rooms.rooms
    ]


def validate_problem(problem: SolveProblem) -> None:
    """Fail loudly on a malformed problem rather than silently dropping a
    constraint (CLAUDE.md invariant 3)."""
    slot_ids = {slot.slot_id for slot in problem.slots}
    rooms_by_id = {room.id: room for room in problem.rooms}
    panels_by_id = {panel.id: panel for panel in problem.panels}

    for panel in problem.panels:
        room = rooms_by_id.get(panel.room)
        if room is None:
            raise ValueError(
                f"Panel '{panel.id}' is in room '{panel.room}', which is not defined in "
                "rooms config — room capacity (C4) could not be enforced for it."
            )
        if panel.division not in room.divisions:
            raise ValueError(
                f"Panel '{panel.id}' has division {panel.division.value} but room "
                f"'{room.id}' is configured for {[d.value for d in room.divisions]} "
                "— fix panels.yaml or rooms.yaml."
            )
        unknown = [s for s in panel.active_slot_ids if s not in slot_ids]
        if unknown:
            raise ValueError(f"Panel '{panel.id}' references slots not on the grid: {unknown}")

    applicants_by_id = {a.applicant_id: a for a in problem.applicants}
    for lock in problem.locks:
        applicant = applicants_by_id.get(lock.applicant_id)
        if applicant is None:
            raise ValueError(f"Lock references unknown applicant '{lock.applicant_id}'.")
        locked_panel = panels_by_id.get(lock.panel_id)
        if locked_panel is None:
            raise ValueError(f"Lock references unknown panel '{lock.panel_id}'.")
        if lock.slot_id not in slot_ids:
            raise ValueError(f"Lock references unknown slot '{lock.slot_id}'.")
        if lock.slot_id not in set(locked_panel.active_slot_ids):
            raise ValueError(
                f"Lock pins {lock.applicant_id}/choice {lock.choice_index} to panel "
                f"'{locked_panel.id}' at slot '{lock.slot_id}', but that panel is not "
                "active then (C7)."
            )
        wanted = applicant.division_1 if lock.choice_index == 1 else applicant.division_2
        if locked_panel.division != wanted:
            raise ValueError(
                f"Lock pins {lock.applicant_id}/choice {lock.choice_index} "
                f"({wanted.value}) to panel '{locked_panel.id}' "
                f"({locked_panel.division.value})."
            )
