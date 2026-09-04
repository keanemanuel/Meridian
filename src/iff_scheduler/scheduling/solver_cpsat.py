"""CP-SAT scheduler — the primary solver (SPEC.md §5.2, §5.4).

Decision variable, exactly as specified:

    x[a, c, p, s] in {0, 1}
      a = applicant
      c = choice index in {1, 2}          <- NOT the division
      p = panel where panel.division == parent_division(a, c)
      s = slot where panel p is active

Indexing by *choice* is load-bearing. Indexing by division would silently
collapse a same-parent pair (Media Marketing + Media Documentation both map
to MEDMARDOC) into a single interview and quietly break FR-30 for those
applicants (SPEC.md §1.2 Finding B; CLAUDE.md invariant 2).

Hard constraints C1-C7 are posted to the model; C8 is soft and auto-relaxing
(FR-30b), carried by the `repeat_panel` objective term. Pure: no I/O, no
adapters, no network.
"""

from __future__ import annotations

import time as timer
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from ortools.sat.python import cp_model

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.models import Applicant, Assignment, ChoiceIndex, Panel, Slot
from iff_scheduler.scheduling.base import (
    USABLE_STATUSES,
    SolveProblem,
    SolveResult,
    validate_problem,
)
from iff_scheduler.scheduling.objectives import c8_applies, panels_by_division


@dataclass(frozen=True)
class _Choice:
    """One (applicant, choice_index) pair — the unit of scheduling (SPEC.md §2)."""

    applicant: Applicant
    choice_index: ChoiceIndex
    sub_division: str
    division: DivisionCode

    @property
    def key(self) -> tuple[str, ChoiceIndex]:
        return (self.applicant.applicant_id, self.choice_index)


def _choices(applicants: Sequence[Applicant]) -> list[_Choice]:
    """Both choices always exist, whether or not they share a parent division (C1)."""
    out: list[_Choice] = []
    for applicant in applicants:
        out.append(_Choice(applicant, 1, applicant.sub_division_1, applicant.division_1))
        out.append(_Choice(applicant, 2, applicant.sub_division_2, applicant.division_2))
    return out


class CpSatSolver:
    """`Solver` implementation backed by OR-Tools CP-SAT."""

    def solve(self, problem: SolveProblem) -> SolveResult:
        """Two-phase solve (SPEC.md §5.4).

        Phase 1 forbids out-of-availability placements outright: if it finds a
        solution, that solution has zero clashes by construction. Phase 2 only
        runs when phase 1 proves there is no zero-clash schedule, and then
        minimises the number of clashes rather than eliminating them (FR-34).
        """
        validate_problem(problem)
        log: list[str] = []
        started = timer.perf_counter()

        if problem.two_phase:
            budget = max(1.0, problem.time_limit_seconds * problem.phase1_time_fraction)
            log.append(
                f"Phase 1 (zero-clash): out-of-availability placements forbidden, "
                f"budget {budget:.1f}s."
            )
            phase1 = self._solve_phase(
                problem, allow_clashes=False, budget=budget, phase=1, log=log
            )
            if phase1.status in USABLE_STATUSES:
                log.append(f"Phase 1 succeeded with 0 clashes ({phase1.status}).")
                return phase1
            log.append(
                f"Phase 1 returned {phase1.status}: no schedule exists inside everyone's "
                "declared availability. Relaxing to phase 2 with clashes penalised."
            )

        elapsed = timer.perf_counter() - started
        budget = max(1.0, problem.time_limit_seconds - elapsed)
        log.append(f"Phase 2 (relaxed): clashes allowed but penalised, budget {budget:.1f}s.")
        return self._solve_phase(problem, allow_clashes=True, budget=budget, phase=2, log=log)

    # ------------------------------------------------------------------ phase

    def _solve_phase(
        self,
        problem: SolveProblem,
        *,
        allow_clashes: bool,
        budget: float,
        phase: int,
        log: list[str],
    ) -> SolveResult:
        started = timer.perf_counter()
        model = cp_model.CpModel()

        choices = _choices(problem.applicants)
        slots_by_id = {slot.slot_id: slot for slot in problem.slots}
        panels_by_id = {panel.id: panel for panel in problem.panels}
        by_division = panels_by_division(problem.panels)
        locked_choices = {(lock.applicant_id, lock.choice_index) for lock in problem.locks}

        # C7 is enforced by construction: a variable only exists for a slot the
        # panel is actually active in, so there is nothing to relax later.
        panel_slots: dict[str, list[Slot]] = {
            panel.id: [s for s in problem.slots if s.slot_id in set(panel.active_slot_ids)]
            for panel in problem.panels
        }

        x: dict[tuple[str, ChoiceIndex, str, str], cp_model.IntVar] = {}
        vars_by_choice: dict[tuple[str, ChoiceIndex], list[cp_model.IntVar]] = defaultdict(list)
        vars_by_panel_slot: dict[tuple[str, str], list[cp_model.IntVar]] = defaultdict(list)
        vars_by_applicant_slot: dict[tuple[str, str], list[cp_model.IntVar]] = defaultdict(list)
        vars_by_choice_panel: dict[tuple[str, ChoiceIndex, str], list[cp_model.IntVar]] = (
            defaultdict(list)
        )
        vars_by_panel: dict[str, list[cp_model.IntVar]] = defaultdict(list)

        for choice in choices:
            applicant_id = choice.applicant.applicant_id
            available = set(choice.applicant.availability_slots)
            # A locked choice is a human decision, not a solver-avoidable clash,
            # so phase 1 must not rule its slot out (C6 outranks the phase filter).
            restrict = not allow_clashes and choice.key not in locked_choices
            for panel in by_division.get(choice.division, []):
                for slot in panel_slots[panel.id]:
                    if restrict and slot.slot_id not in available:
                        continue
                    key = (applicant_id, choice.choice_index, panel.id, slot.slot_id)
                    var = model.new_bool_var(
                        f"x_{applicant_id}_{choice.choice_index}_{panel.id}_{slot.slot_id}"
                    )
                    x[key] = var
                    vars_by_choice[choice.key].append(var)
                    vars_by_panel_slot[(panel.id, slot.slot_id)].append(var)
                    vars_by_applicant_slot[(applicant_id, slot.slot_id)].append(var)
                    vars_by_choice_panel[(applicant_id, choice.choice_index, panel.id)].append(var)
                    vars_by_panel[panel.id].append(var)

        # C1 — completeness (FR-30). A choice with no candidate placement makes
        # the instance infeasible; say which one rather than letting CP-SAT
        # report a bare INFEASIBLE (E-18).
        for choice in choices:
            candidates = vars_by_choice[choice.key]
            if not candidates:
                reason = (
                    "has no panel of that division active in any of their declared slots"
                    if not allow_clashes
                    else f"has no {choice.division.value} panel with any active slot"
                )
                log.append(
                    f"INFEASIBLE (C1): {choice.applicant.applicant_id} choice "
                    f"{choice.choice_index} ({choice.sub_division}) {reason}."
                )
                return SolveResult(
                    assignments=[],
                    status="INFEASIBLE",
                    objective_value=0,
                    clash_count=0,
                    solve_seconds=timer.perf_counter() - started,
                    phase=phase,
                    log=list(log),
                )
            model.add_exactly_one(candidates)

        # C2 — panel exclusivity (FR-23).
        for panel_slot_vars in vars_by_panel_slot.values():
            if len(panel_slot_vars) > 1:
                model.add_at_most_one(panel_slot_vars)

        # C3 — applicant exclusivity (FR-31).
        for applicant_slot_vars in vars_by_applicant_slot.values():
            if len(applicant_slot_vars) > 1:
                model.add_at_most_one(applicant_slot_vars)

        # C4 — room concurrency (FR-24).
        panels_in_room: dict[str, list[Panel]] = defaultdict(list)
        for panel in problem.panels:
            panels_in_room[panel.room].append(panel)
        for room in problem.rooms:
            room_panels = panels_in_room.get(room.id, [])
            if len(room_panels) <= room.max_concurrent_panels:
                continue  # C2 already caps the room at one interview per panel
            for slot in problem.slots:
                here = [
                    var
                    for panel in room_panels
                    for var in vars_by_panel_slot.get((panel.id, slot.slot_id), [])
                ]
                if here:
                    model.add(sum(here) <= room.max_concurrent_panels)

        # C5 — minimum gap (FR-32). `min_gap_slots` counts *free* slots between an
        # applicant's two interviews, so 0 permits back-to-back and 1 leaves one
        # slot of travel time (E-07). Windows never span a day boundary: the last
        # slot of Thursday and the first of Friday are hours apart, not adjacent.
        if problem.min_gap_slots > 0:
            self._add_gap_constraints(model, problem, vars_by_applicant_slot)

        # C6 — locks (FR-41).
        for lock in problem.locks:
            key = (lock.applicant_id, lock.choice_index, lock.panel_id, lock.slot_id)
            locked_var = x.get(key)
            if locked_var is None:
                log.append(
                    f"INFEASIBLE (C6): lock {lock.applicant_id}/choice {lock.choice_index} "
                    f"-> {lock.panel_id} @ {lock.slot_id} has no corresponding placement."
                )
                return SolveResult(
                    assignments=[],
                    status="INFEASIBLE",
                    objective_value=0,
                    clash_count=0,
                    solve_seconds=timer.perf_counter() - started,
                    phase=phase,
                    log=list(log),
                )
            model.add(locked_var == 1)

        objective = self._build_objective(
            model,
            problem,
            x=x,
            choices=choices,
            by_division=by_division,
            vars_by_choice_panel=vars_by_choice_panel,
            vars_by_panel=vars_by_panel,
            slots_by_id=slots_by_id,
        )
        model.minimize(objective)

        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = budget
        solver.parameters.random_seed = problem.random_seed
        # FR-35: a single deterministic worker means identical inputs give an
        # identical schedule, not merely an identical objective value.
        solver.parameters.num_search_workers = 1
        status = solver.solve(model)
        status_name = solver.status_name(status)
        solve_seconds = timer.perf_counter() - started

        if status_name not in USABLE_STATUSES:
            log.append(f"Phase {phase}: CP-SAT returned {status_name} after {solve_seconds:.2f}s.")
            return SolveResult(
                assignments=[],
                status=status_name,
                objective_value=0,
                clash_count=0,
                solve_seconds=solve_seconds,
                phase=phase,
                log=list(log),
            )

        assignments = self._extract_assignments(
            solver,
            problem,
            x=x,
            choices=choices,
            by_division=by_division,
            panels_by_id=panels_by_id,
            slots_by_id=slots_by_id,
            panel_slots=panel_slots,
            locked_choices=locked_choices,
        )
        clash_count = sum(1 for a in assignments if a.is_clash)
        objective_value = int(solver.objective_value)
        log.append(
            f"Phase {phase}: {status_name} in {solve_seconds:.2f}s — "
            f"{len(assignments)} interviews, {clash_count} clash(es), "
            f"objective {objective_value}."
        )
        if status_name != "OPTIMAL":
            # E-17: the time limit was hit. Report the best solution found with
            # its optimality gap rather than returning nothing — and say so,
            # because a time-limited solve is only reproducible on a machine of
            # comparable speed (FR-35 holds exactly when optimality is proven).
            bound = int(solver.best_objective_bound)
            log.append(
                f"Phase {phase}: time limit reached before proving optimality. Best "
                f"objective {objective_value}, proven bound {bound} "
                f"(gap {objective_value - bound})."
            )
        return SolveResult(
            assignments=assignments,
            status=status_name,
            objective_value=objective_value,
            clash_count=clash_count,
            solve_seconds=solve_seconds,
            phase=phase,
            log=list(log),
        )

    # ------------------------------------------------------------ constraints

    @staticmethod
    def _add_gap_constraints(
        model: cp_model.CpModel,
        problem: SolveProblem,
        vars_by_applicant_slot: dict[tuple[str, str], list[cp_model.IntVar]],
    ) -> None:
        """C5: at most one interview per applicant in any window of
        `min_gap_slots + 1` consecutive slots on the same day."""
        slots_by_day: dict[object, list[Slot]] = defaultdict(list)
        for slot in problem.slots:
            slots_by_day[slot.date].append(slot)

        width = problem.min_gap_slots + 1
        for applicant in problem.applicants:
            for day_slots in slots_by_day.values():
                for start in range(len(day_slots) - width + 1):
                    window = day_slots[start : start + width]
                    here = [
                        var
                        for slot in window
                        for var in vars_by_applicant_slot.get(
                            (applicant.applicant_id, slot.slot_id), []
                        )
                    ]
                    if len(here) > 1:
                        model.add_at_most_one(here)

    # ------------------------------------------------------------- objective

    @staticmethod
    def _build_objective(
        model: cp_model.CpModel,
        problem: SolveProblem,
        *,
        x: dict[tuple[str, ChoiceIndex, str, str], cp_model.IntVar],
        choices: Sequence[_Choice],
        by_division: dict[DivisionCode, list[Panel]],
        vars_by_choice_panel: dict[tuple[str, ChoiceIndex, str], list[cp_model.IntVar]],
        vars_by_panel: dict[str, list[cp_model.IntVar]],
        slots_by_id: dict[str, Slot],
    ) -> cp_model.LinearExpr:
        """The SPEC.md §5.2 objective, term for term.

        Mirrors `objectives.score_schedule` exactly, so the CP-SAT objective
        value and the independently-computed breakdown agree.
        """
        weights = problem.weights
        terms: list[cp_model.LinearExpr] = []

        # W_CLASH (dominant) and W_LATE, both linear in x.
        availability = {a.applicant_id: set(a.availability_slots) for a in problem.applicants}
        for (applicant_id, _choice_index, _panel_id, slot_id), var in x.items():
            coefficient = weights.lateness * slots_by_id[slot_id].slot_index
            if slot_id not in availability[applicant_id]:
                coefficient += weights.clash
            if coefficient:
                terms.append(coefficient * var)

        # W_REPEAT — C8, soft and auto-relaxing (FR-30b, E-01c).
        for applicant in problem.applicants:
            if not c8_applies(applicant, by_division):
                continue
            for panel in by_division[applicant.division_1]:
                first = vars_by_choice_panel.get((applicant.applicant_id, 1, panel.id), [])
                second = vars_by_choice_panel.get((applicant.applicant_id, 2, panel.id), [])
                if not first or not second:
                    continue
                repeat = model.new_bool_var(f"repeat_{applicant.applicant_id}_{panel.id}")
                model.add(sum(first) + sum(second) - 1 <= repeat)
                terms.append(weights.repeat_panel * repeat)

        # W_SPREAD — dead time between an applicant's two interviews (FR-36).
        last_index = len(problem.slots) - 1
        for applicant in problem.applicants:
            positions = []
            for choice_index in (1, 2):
                placed = [
                    var
                    for panel in by_division.get(
                        applicant.division_1 if choice_index == 1 else applicant.division_2, []
                    )
                    for var in vars_by_choice_panel.get(
                        (applicant.applicant_id, choice_index, panel.id), []
                    )
                ]
                if not placed:
                    break
                position = model.new_int_var(
                    0, last_index, f"pos_{applicant.applicant_id}_{choice_index}"
                )
                model.add(
                    position
                    == sum(
                        slots_by_id[key[3]].slot_index * var
                        for key, var in x.items()
                        if key[0] == applicant.applicant_id and key[1] == choice_index
                    )
                )
                positions.append(position)
            if len(positions) == 2:
                gap = model.new_int_var(0, last_index, f"gap_{applicant.applicant_id}")
                model.add_abs_equality(gap, positions[0] - positions[1])
                terms.append(weights.spread * gap)

        # W_BALANCE — spread of load across panels of the same division (FR-37).
        for division_panels in by_division.values():
            if len(division_panels) < 2:
                continue
            loads = []
            for panel in division_panels:
                load = model.new_int_var(0, len(choices), f"load_{panel.id}")
                model.add(load == sum(vars_by_panel.get(panel.id, [])))
                loads.append(load)
            highest = model.new_int_var(0, len(choices), f"maxload_{division_panels[0].division}")
            lowest = model.new_int_var(0, len(choices), f"minload_{division_panels[0].division}")
            model.add_max_equality(highest, loads)
            model.add_min_equality(lowest, loads)
            terms.append(weights.balance * (highest - lowest))

        return cp_model.LinearExpr.sum(terms) if terms else cp_model.LinearExpr.sum([])

    # ------------------------------------------------------------- extraction

    @staticmethod
    def _extract_assignments(
        solver: cp_model.CpSolver,
        problem: SolveProblem,
        *,
        x: dict[tuple[str, ChoiceIndex, str, str], cp_model.IntVar],
        choices: Sequence[_Choice],
        by_division: dict[DivisionCode, list[Panel]],
        panels_by_id: dict[str, Panel],
        slots_by_id: dict[str, Slot],
        panel_slots: dict[str, list[Slot]],
        locked_choices: set[tuple[str, ChoiceIndex]],
    ) -> list[Assignment]:
        """Read the solution back out as domain `Assignment`s, ordered
        deterministically by (applicant_id, choice_index) — FR-35."""
        assignments: list[Assignment] = []
        for choice in sorted(choices, key=lambda c: (c.applicant.applicant_id, c.choice_index)):
            applicant = choice.applicant
            chosen: tuple[str, str] | None = None
            for panel in by_division.get(choice.division, []):
                for slot in panel_slots[panel.id]:
                    key = (applicant.applicant_id, choice.choice_index, panel.id, slot.slot_id)
                    var = x.get(key)
                    if var is not None and solver.value(var):
                        chosen = (panel.id, slot.slot_id)
                        break
                if chosen:
                    break
            if chosen is None:  # pragma: no cover - C1 makes this unreachable
                raise RuntimeError(
                    f"C1 violated: {applicant.applicant_id} choice {choice.choice_index} "
                    "came back unplaced from a feasible solve."
                )

            panel_id, slot_id = chosen
            panel = panels_by_id[panel_id]
            slot = slots_by_id[slot_id]
            is_clash = slot_id not in set(applicant.availability_slots)
            assignments.append(
                Assignment(
                    applicant_id=applicant.applicant_id,
                    full_name=applicant.full_name,
                    email=applicant.email,
                    choice_index=choice.choice_index,
                    sub_division=choice.sub_division,
                    division=choice.division,
                    panel_id=panel_id,
                    room=panel.room,
                    slot_id=slot_id,
                    date=slot.date,
                    start_time=slot.start_time,
                    end_time=slot.end_time,
                    is_clash=is_clash,
                    is_locked=choice.key in locked_choices,
                    same_parent_pair=applicant.division_1 == applicant.division_2,
                    reason=(
                        _clash_reason(applicant, choice, by_division, panel_slots)
                        if is_clash
                        else None
                    ),
                )
            )
        return assignments


def _clash_reason(
    applicant: Applicant,
    choice: _Choice,
    by_division: dict[DivisionCode, list[Panel]],
    panel_slots: dict[str, list[Slot]],
) -> str:
    """Explain *why* this interview had to go outside declared availability.

    "Log decisions, not noise" (CLAUDE.md): which applicant, which choice, and
    why no in-availability slot existed.
    """
    available = set(applicant.availability_slots)
    reachable = {
        slot.slot_id
        for panel in by_division.get(choice.division, [])
        for slot in panel_slots[panel.id]
        if slot.slot_id in available
    }
    division = choice.division.value
    if not reachable:
        return (
            f"CLASH: no {division} panel is active during any of this applicant's "
            f"{len(available)} declared slot(s)."
        )
    return (
        f"CLASH: all {len(reachable)} in-availability {division} panel-slot(s) were "
        "taken by other applicants or blocked by this applicant's other interview."
    )


def solve(problem: SolveProblem) -> SolveResult:
    """Module-level convenience wrapper around `CpSatSolver`."""
    return CpSatSolver().solve(problem)
