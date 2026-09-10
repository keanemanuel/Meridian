"""Penalty weights and schedule scoring (SPEC.md §5.2, "Objective").

Pure and solver-agnostic: this module scores a finished schedule, so the
CP-SAT solver and the greedy fallback (§5.3) can be compared on the same
number. `solver_cpsat` builds the identical expression inside the CP-SAT
model; `test_solver_constraints` asserts the two agree.

The weight ordering `clash >> different_day > repeat_panel > spread > balance
> subdivision_switch > lateness` makes the objective lexicographic in
practice: the solver will never accept an extra clash to gain compactness,
never split an applicant's two interviews across days to avoid a repeated
panel, and never drop an interview at all (FR-33, FR-36b). `subdivision_switch`
is the Part 2 clustering nudge — keep each sub-division of a shared-panel
division (Creative / WebMaster) in a contiguous block on a panel's day, or on
its own panel/room where capacity allows — ranked low so it only breaks ties.
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
    different_days: int
    repeat_panels: int
    spread_slots: int
    # The widest gap (free slots between, over `min_gap_slots`) any single
    # applicant has between two *same-day* interviews — an L-infinity / bottleneck
    # term (FR-36). Minimising it stops the solver from leaving one applicant a
    # long "gap between classes" while others go back-to-back. Pre-weight, like
    # every other count here; `same_day_gap` in `weights` scales it into `total`.
    same_day_max_gap: int
    balance_spread: int
    subdivision_switches: int
    lateness: int
    weights: SolverWeights

    @property
    def total(self) -> int:
        return (
            self.weights.clash * self.clashes
            + self.weights.different_day * self.different_days
            + self.weights.repeat_panel * self.repeat_panels
            + self.weights.spread * self.spread_slots
            + self.weights.same_day_gap * self.same_day_max_gap
            + self.weights.balance * self.balance_spread
            + self.weights.subdivision_switch * self.subdivision_switches
            + self.weights.lateness * self.lateness
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "clashes": self.clashes,
            "different_days": self.different_days,
            "repeat_panels": self.repeat_panels,
            "spread_slots": self.spread_slots,
            "same_day_max_gap": self.same_day_max_gap,
            "balance_spread": self.balance_spread,
            "subdivision_switches": self.subdivision_switches,
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
    if applicant.division_2 is None or applicant.division_1 != applicant.division_2:
        return False
    return len(by_division.get(applicant.division_1, [])) >= 2


def count_subdivision_switches(
    assignments: Sequence[Assignment],
    panels: Sequence[Panel],
    slots: Sequence[Slot],
) -> int:
    """Times a panel's running order steps from one sub-division to another
    between two consecutive-on-the-grid, same-day slots (Part 2 clustering).

    Only counted for divisions that actually run more than one sub-division in
    this schedule — a division with a single sub-division (Logistics, Liaison)
    can never switch and is never charged for it. Phase 2 of the CP-SAT solve
    builds the identical quantity; phase 1 (zero-clash) omits it, so callers
    pass `subdivision_switch_scored=False` for a phase-1 result and the
    breakdown total still matches `objective_value`.
    """
    slot_by_id = {slot.slot_id: slot for slot in slots}
    division_of_panel = {panel.id: panel.division for panel in panels}

    subdivisions: dict[DivisionCode, set[str]] = defaultdict(set)
    for assignment in assignments:
        subdivisions[assignment.division].add(assignment.sub_division)

    by_panel: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        by_panel[assignment.panel_id].append(assignment)

    switches = 0
    for panel_id, booked in by_panel.items():
        division = division_of_panel.get(panel_id)
        if division is None or len(subdivisions.get(division, set())) < 2:
            continue
        ordered = sorted(booked, key=lambda a: slot_by_id[a.slot_id].slot_index)
        for i in range(len(ordered) - 1):
            prev, cur = ordered[i], ordered[i + 1]
            prev_slot, cur_slot = slot_by_id[prev.slot_id], slot_by_id[cur.slot_id]
            if prev_slot.date != cur_slot.date:
                continue
            if cur_slot.slot_index != prev_slot.slot_index + 1:
                continue
            if prev.sub_division != cur.sub_division:
                switches += 1
    return switches


def score_schedule(
    assignments: Sequence[Assignment],
    applicants: Sequence[Applicant],
    panels: Sequence[Panel],
    slots: Sequence[Slot],
    weights: SolverWeights,
    *,
    min_gap_slots: int = 0,
    subdivision_switch_scored: bool = True,
    same_day_gap_scored: bool = True,
) -> ObjectiveBreakdown:
    """Score a finished schedule against the SPEC.md §5.2 objective.

    `subdivision_switch_scored` and `same_day_gap_scored` must match whether the
    solve actually put those terms in its model — both are off for a phase-1
    (zero-clash) result, so the breakdown total still equals what CP-SAT
    minimised. Pass `result.phase == 2` from the pipeline.

    `min_gap_slots` must be the solve's own value: the same-day gap term only
    charges for slots *beyond* the gap C5 already forces, so the two agree.
    """
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
    same_day_max_gap = 0
    different_days = 0
    for applicant in applicants:
        theirs = per_applicant.get(applicant.applicant_id, [])
        if len(theirs) != 2:
            continue
        first, second = theirs
        distance = abs(slot_index[first.slot_id] - slot_index[second.slot_id])
        spread_slots += distance
        if first.date != second.date:
            different_days += 1
        elif same_day_gap_scored:
            # free slots between the two interviews, minus the gap C5 forces;
            # the term tracks the single widest such gap in the schedule.
            excess = max(0, distance - 1 - min_gap_slots)
            same_day_max_gap = max(same_day_max_gap, excess)
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
        different_days=different_days,
        repeat_panels=repeat_panels,
        spread_slots=spread_slots,
        same_day_max_gap=same_day_max_gap,
        balance_spread=balance_spread,
        subdivision_switches=(
            count_subdivision_switches(assignments, panels, slots)
            if subdivision_switch_scored
            else 0
        ),
        lateness=lateness,
        weights=weights,
    )
