"""Penalty weights and schedule scoring (SPEC.md §5.2, "Objective").

Pure and solver-agnostic: this module scores a finished schedule, so the
CP-SAT solver and the greedy fallback (§5.3) can be compared on the same
number. `solver_cpsat` builds the identical expression inside the CP-SAT
model; `test_solver_constraints` asserts the two agree.

The weight ordering `clash >> repeat_panel > spread > balance > lateness`
makes the objective lexicographic in practice: the solver will never accept
an extra clash to gain compactness, and never drop an interview to avoid a
repeated panel (FR-33).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.models import Applicant, Assignment, Panel, Slot
from iff_scheduler.settings import SolverWeights


@dataclass(frozen=True)
class ObjectiveBreakdown:
    """Raw counts per objective term, plus the weighted total.

    Keeping the raw counts alongside the total is what lets `metrics.json`
    answer "why is this schedule worse than yesterday's" rather than only
    "it scores 41 380".
    """

    clashes: int
    repeat_panels: int
    spread_slots: int
    balance_spread: int
    lateness: int
    weights: SolverWeights

    @property
    def total(self) -> int:
        return (
            self.weights.clash * self.clashes
            + self.weights.repeat_panel * self.repeat_panels
            + self.weights.spread * self.spread_slots
            + self.weights.balance * self.balance_spread
            + self.weights.lateness * self.lateness
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "clashes": self.clashes,
            "repeat_panels": self.repeat_panels,
            "spread_slots": self.spread_slots,
            "balance_spread": self.balance_spread,
            "lateness": self.lateness,
            "total": self.total,
        }


def panels_by_division(panels: Sequence[Panel]) -> dict[DivisionCode, list[Panel]]:
    grouped: dict[DivisionCode, list[Panel]] = defaultdict(list)
    for panel in panels:
        grouped[panel.division].append(panel)
    return dict(grouped)


def c8_applies(applicant: Applicant, by_division: dict[DivisionCode, list[Panel]]) -> bool:
    """C8 is live only for a same-parent pair whose division has >= 2 panels.

    With one panel the preference is dropped entirely rather than made
    infeasible (E-01c) — and, crucially, it is not *scored* either, so a
    single-panel division is not penalised for something it cannot avoid.
    """
    if applicant.division_1 != applicant.division_2:
        return False
    return len(by_division.get(applicant.division_1, [])) >= 2


def score_schedule(
    assignments: Sequence[Assignment],
    applicants: Sequence[Applicant],
    panels: Sequence[Panel],
    slots: Sequence[Slot],
    weights: SolverWeights,
) -> ObjectiveBreakdown:
    """Score a finished schedule against the SPEC.md §5.2 objective."""
    slot_index = {slot.slot_id: slot.slot_index for slot in slots}
    by_division = panels_by_division(panels)
    availability = {a.applicant_id: set(a.availability_slots) for a in applicants}

    clashes = 0
    lateness = 0
    per_applicant: dict[str, list[Assignment]] = defaultdict(list)
    load: dict[str, int] = {panel.id: 0 for panel in panels}

    for assignment in assignments:
        if assignment.slot_id not in availability.get(assignment.applicant_id, set()):
            clashes += 1
        lateness += slot_index[assignment.slot_id]
        per_applicant[assignment.applicant_id].append(assignment)
        load[assignment.panel_id] = load.get(assignment.panel_id, 0) + 1

    repeat_panels = 0
    spread_slots = 0
    for applicant in applicants:
        theirs = per_applicant.get(applicant.applicant_id, [])
        if len(theirs) != 2:
            continue
        first, second = theirs
        spread_slots += abs(slot_index[first.slot_id] - slot_index[second.slot_id])
        if first.panel_id == second.panel_id and c8_applies(applicant, by_division):
            repeat_panels += 1

    balance_spread = 0
    for division_panels in by_division.values():
        if len(division_panels) < 2:
            continue
        loads = [load[panel.id] for panel in division_panels]
        balance_spread += max(loads) - min(loads)

    return ObjectiveBreakdown(
        clashes=clashes,
        repeat_panels=repeat_panels,
        spread_slots=spread_slots,
        balance_spread=balance_spread,
        lateness=lateness,
        weights=weights,
    )
