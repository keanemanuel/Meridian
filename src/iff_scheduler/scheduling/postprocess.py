"""Clash detection, run metrics and schedule diffing (SPEC.md §6.3, FR-43, FR-44).

Runs after a solve, on plain domain objects. Pure — the CLI decides where any
of this gets written; this module only decides what it says.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from iff_scheduler.domain.enums import Severity
from iff_scheduler.domain.models import (
    Applicant,
    Assignment,
    ChoiceIndex,
    Conflict,
    Panel,
    Schedule,
)
from iff_scheduler.scheduling.base import SolveProblem, SolveResult
from iff_scheduler.scheduling.objectives import panels_by_division, score_schedule

ChangeKind = Literal["ADDED", "REMOVED", "MOVED"]


@dataclass(frozen=True)
class AssignmentChange:
    """One line of the diff between two solves (FR-44)."""

    applicant_id: str
    choice_index: ChoiceIndex
    change: ChangeKind
    before: str | None
    after: str | None


def _placement(assignment: Assignment) -> str:
    return f"{assignment.panel_id} @ {assignment.slot_id}"


def build_conflicts(
    assignments: Sequence[Assignment],
    applicants: Sequence[Applicant],
    panels: Sequence[Panel],
) -> list[Conflict]:
    """The conflict report: clashes, unfilled requirements, capacity warnings (FR-43)."""
    conflicts: list[Conflict] = []
    by_division = panels_by_division(panels)
    per_applicant: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        per_applicant[assignment.applicant_id].append(assignment)

    for applicant in applicants:
        theirs = per_applicant.get(applicant.applicant_id, [])

        # Unfilled requirement — a C1 violation, which should be impossible on a
        # feasible solve, so it is reported loudly rather than assumed away.
        if len(theirs) != 2:
            conflicts.append(
                Conflict(
                    applicant_id=applicant.applicant_id,
                    severity=Severity.RED,
                    type="UNFILLED",
                    message=(
                        f"{len(theirs)} interview(s) scheduled, expected 2 "
                        "(FR-30 is a hard guarantee)."
                    ),
                )
            )
            continue

        for assignment in sorted(theirs, key=lambda a: a.choice_index):
            if assignment.is_clash:
                conflicts.append(
                    Conflict(
                        applicant_id=applicant.applicant_id,
                        severity=Severity.RED,
                        type="CLASH",
                        message=(
                            f"Choice {assignment.choice_index} ({assignment.sub_division}) "
                            f"placed at {assignment.slot_id} on {assignment.panel_id}, "
                            f"outside declared availability. "
                            f"{assignment.reason or ''}".strip()
                        ),
                    )
                )

        # E-01c: a same-parent pair whose division has only one panel sees that
        # panel twice. Legal, but the recruiter should brief the panel.
        first, second = sorted(theirs, key=lambda a: a.choice_index)
        if first.panel_id == second.panel_id:
            panel_count = len(by_division.get(applicant.division_1, []))
            conflicts.append(
                Conflict(
                    applicant_id=applicant.applicant_id,
                    severity=Severity.AMBER,
                    type="REPEAT_PANEL",
                    message=(
                        f"Both interviews are with {first.panel_id} "
                        f"({first.sub_division} and {second.sub_division}); "
                        + (
                            f"{applicant.division_1.value} has only one panel, so C8 was "
                            "auto-relaxed (E-01c). Brief the panel."
                            if panel_count < 2
                            else "brief the panel."
                        )
                    ),
                )
            )

    # Capacity warning: a panel with no idle slot left has no room to absorb a
    # manual edit later (FR-43, and it is what M5's edit validator will bounce).
    load = Counter(a.panel_id for a in assignments)
    for panel in panels:
        capacity = len(panel.active_slot_ids)
        if capacity and load[panel.id] >= capacity:
            conflicts.append(
                Conflict(
                    applicant_id="",
                    severity=Severity.AMBER,
                    type="PANEL_SATURATED",
                    message=(
                        f"Panel {panel.id} is full: {load[panel.id]}/{capacity} active slots "
                        "used. A manual move onto this panel will be rejected."
                    ),
                )
            )

    return conflicts


def compute_metrics(result: SolveResult, problem: SolveProblem) -> dict[str, Any]:
    """metrics.json for the run directory — enough to explain the schedule later."""
    breakdown = score_schedule(
        result.assignments, problem.applicants, problem.panels, problem.slots, problem.weights
    )

    load = Counter(a.panel_id for a in result.assignments)
    per_panel = {
        panel.id: {
            "division": panel.division.value,
            "room": panel.room,
            "interviews": load[panel.id],
            "active_slots": len(panel.active_slot_ids),
            "idle_slots": max(0, len(panel.active_slot_ids) - load[panel.id]),
        }
        for panel in problem.panels
    }
    per_division = Counter(a.division.value for a in result.assignments)
    per_slot = Counter(a.slot_id for a in result.assignments)

    return {
        "status": result.status,
        "phase": result.phase,
        "solve_seconds": round(result.solve_seconds, 3),
        "objective_value": result.objective_value,
        "objective_breakdown": breakdown.as_dict(),
        "applicants": len(problem.applicants),
        "interviews_required": 2 * len(problem.applicants),
        "interviews_placed": len(result.assignments),
        "clashes": result.clash_count,
        "locked": sum(1 for a in result.assignments if a.is_locked),
        "same_parent_pairs": sum(1 for a in problem.applicants if a.division_1 == a.division_2),
        "slots": len(problem.slots),
        "panels": len(problem.panels),
        "per_division_interviews": dict(sorted(per_division.items())),
        "per_panel": per_panel,
        "peak_slot_occupancy": max(per_slot.values()) if per_slot else 0,
        "log": result.log,
    }


def diff_schedules(
    previous: Sequence[Assignment], current: Sequence[Assignment]
) -> list[AssignmentChange]:
    """What moved between two solves (FR-44). Unchanged rows are omitted."""
    before = {(a.applicant_id, a.choice_index): a for a in previous}
    after = {(a.applicant_id, a.choice_index): a for a in current}

    changes: list[AssignmentChange] = []
    for key in sorted(set(before) | set(after)):
        applicant_id, choice_index = key
        old, new = before.get(key), after.get(key)
        if old is None and new is not None:
            changes.append(
                AssignmentChange(applicant_id, choice_index, "ADDED", None, _placement(new))
            )
        elif new is None and old is not None:
            changes.append(
                AssignmentChange(applicant_id, choice_index, "REMOVED", _placement(old), None)
            )
        elif old is not None and new is not None and _placement(old) != _placement(new):
            changes.append(
                AssignmentChange(
                    applicant_id, choice_index, "MOVED", _placement(old), _placement(new)
                )
            )
    return changes


def build_schedule(run_id: str, result: SolveResult, problem: SolveProblem) -> Schedule:
    """Bundle a solve result and its conflict report into one immutable Schedule."""
    return Schedule(
        run_id=run_id,
        assignments=list(result.assignments),
        conflicts=build_conflicts(result.assignments, problem.applicants, problem.panels),
    )
