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
from datetime import date as Date

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
    """One `_Choice` per interview the applicant is owed: two for a normal
    applicant (whether or not the choices share a parent division, C1), one
    for a single-choice applicant who picked only one role."""
    out: list[_Choice] = []
    for applicant in applicants:
        out.append(_Choice(applicant, 1, applicant.sub_division_1, applicant.division_1))
        if not applicant.single_choice and applicant.division_2 is not None:
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
        vars_by_choice_day: dict[tuple[str, ChoiceIndex, Date], list[cp_model.IntVar]] = (
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
                    vars_by_choice_day[(applicant_id, choice.choice_index, slot.date)].append(var)
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
            vars_by_choice_day=vars_by_choice_day,
            vars_by_panel=vars_by_panel,
            slots_by_id=slots_by_id,
            score_subdivision_switch=allow_clashes,
            score_same_day_gap=allow_clashes,
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
        vars_by_choice_day: dict[tuple[str, ChoiceIndex, Date], list[cp_model.IntVar]],
        vars_by_panel: dict[str, list[cp_model.IntVar]],
        slots_by_id: dict[str, Slot],
        score_subdivision_switch: bool = True,
        score_same_day_gap: bool = True,
    ) -> cp_model.LinearExpr:
        """The SPEC.md §5.2 objective, term for term.

        Mirrors `objectives.score_schedule` exactly, so the CP-SAT objective
        value and the independently-computed breakdown agree.

        `score_subdivision_switch` and `score_same_day_gap` are both False for
        phase 1 (the zero-clash phase): that phase is time-limited against the
        240-interview budget (FR-39) and returns the first optimal/feasible
        schedule it finds, so its model is kept exactly as it was rather than
        loaded with a low-priority refinement (the Part 2 clustering term or the
        Part 3 same-day gap term). Both ride on phase 2, where the schedule is
        already relaxed and the solver has headroom. `score_schedule` is told
        the same via its `subdivision_switch_scored` / `same_day_gap_scored`
        flags, so the breakdown still matches whichever phase produced the
        result.
        """
        weights = problem.weights
        # `int` entries occur when a soft term collapses to a constant (e.g. a
        # pair with no common candidate day always pays W_DIFFERENT_DAY);
        # LinearExpr.sum folds them in and score_schedule counts the same.
        terms: list[cp_model.LinearExpr | int] = []

        # W_CLASH (dominant) and W_LATE, both linear in x.
        availability = {a.applicant_id: set(a.availability_slots) for a in problem.applicants}
        for (applicant_id, _choice_index, _panel_id, slot_id), var in x.items():
            coefficient = weights.lateness * slots_by_id[slot_id].slot_index
            if slot_id not in availability[applicant_id]:
                coefficient += weights.clash
            if coefficient:
                terms.append(coefficient * var)

        # W_DIFFERENT_DAY — both of an applicant's interviews on one event day
        # (FR-36b). `same_on[d]` is the AND of "choice 1 lands on day d" and
        # "choice 2 lands on day d" (each a 0/1 sum by C1); it can be 1 for at
        # most one day, so `different_day` is forced to 1 exactly when no day
        # holds both. Soft: ranked above repeat_panel and spread but far below
        # clash, so a pair is split across days only when every same-day
        # placement would require a clash. An applicant whose two choices have
        # no common candidate day is split by construction — the term is still
        # added (as a constant) so the objective value matches score_schedule.
        # `same_day_sum[aid]` (a 0/1 expression: at most one per-day flag is hot
        # by C1) is captured here and reused by W_SAME_DAY_GAP below, so that
        # term adds no per-day vars of its own.
        event_dates = sorted({slot.date for slot in problem.slots})
        same_day_sum: dict[str, cp_model.LinearExpr | int] = {}
        if weights.different_day and len(event_dates) > 1:
            for applicant in problem.applicants:
                if applicant.single_choice or applicant.division_2 is None:
                    continue
                aid = applicant.applicant_id
                same_day_flags: list[cp_model.IntVar] = []
                for day in event_dates:
                    first = vars_by_choice_day.get((aid, 1, day), [])
                    second = vars_by_choice_day.get((aid, 2, day), [])
                    if not first or not second:
                        continue
                    same_on_day = model.new_bool_var(f"sameday_{aid}_{day.isoformat()}")
                    model.add(same_on_day <= sum(first))
                    model.add(same_on_day <= sum(second))
                    model.add(same_on_day >= sum(first) + sum(second) - 1)
                    same_day_flags.append(same_on_day)
                different_day = model.new_bool_var(f"diffday_{aid}")
                model.add(different_day + sum(same_day_flags) >= 1)
                terms.append(weights.different_day * different_day)
                same_day_sum[aid] = sum(same_day_flags) if same_day_flags else 0

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

        # W_SPREAD — total dead time between an applicant's two interviews
        # (FR-36), linear in the raw grid distance (an L1 term: keeps the sum of
        # gaps down). W_SAME_DAY_GAP — the Part 3 refinement: a *bottleneck*
        # (L-infinity) term on the single widest gap any applicant has between
        # two interviews **on the same event day**. Minimising the worst gap is
        # what stops the "5-hour gap between classes" — one applicant left with a
        # long wait while everyone else goes back-to-back. Together the two keep
        # same-day gaps both small (W_SPREAD) and even (W_SAME_DAY_GAP), which is
        # Strategy 1 from the brief; Strategy 2 ("both as early as possible") is
        # already served by the untouched W_LATE term.
        #
        # Soft, and kept below W_DIFF_DAY: `worst_same_day_gap` is a single int
        # var in [0, last_index], and with `same_day_gap` small enough that
        # `same_day_gap · last_index < different_day` (see config/solver.yaml) it
        # can never make a cross-day split look cheaper than a same-day pair —
        # FR-36b keeps priority. A gap forced wide by panel/room availability
        # just pins `worst_same_day_gap` there; the term then stops discriminating
        # but never forces a clash or a cross-day split to shrink it.
        #
        # Like the Part 2 clustering term it rides on phase 2 only
        # (`score_same_day_gap`): phase 1 is time-boxed against the 240-interview
        # budget (FR-39) and already minimises the linear W_SPREAD, so its model
        # is left exactly as it was.
        # A single-choice applicant has no second interview, so there is no
        # gap to penalise (the `not placed` break also covers this).
        last_index = len(problem.slots) - 1
        multi_day = len({slot.date for slot in problem.slots}) > 1
        price_same_day_gap = score_same_day_gap and weights.same_day_gap > 0
        worst_same_day_gap = (
            model.new_int_var(0, last_index, "worst_same_day_gap")
            if price_same_day_gap
            else None
        )
        for applicant in problem.applicants:
            aid = applicant.applicant_id
            positions = []
            for choice_index in (1, 2):
                division = applicant.division_1 if choice_index == 1 else applicant.division_2
                placed = (
                    [
                        var
                        for panel in by_division.get(division, [])
                        for var in vars_by_choice_panel.get(
                            (aid, choice_index, panel.id), []
                        )
                    ]
                    if division is not None
                    else []
                )
                if not placed:
                    break
                position = model.new_int_var(0, last_index, f"pos_{aid}_{choice_index}")
                model.add(
                    position
                    == sum(
                        slots_by_id[key[3]].slot_index * var
                        for key, var in x.items()
                        if key[0] == aid and key[1] == choice_index
                    )
                )
                positions.append(position)
            if len(positions) != 2:
                continue

            gap = model.new_int_var(0, last_index, f"gap_{aid}")
            model.add_abs_equality(gap, positions[0] - positions[1])
            if weights.spread:
                terms.append(weights.spread * gap)

            if worst_same_day_gap is None:
                continue
            # `same_day` == 1 iff both interviews sit on one event day. Reused
            # from W_DIFF_DAY where available (the common case); on a single-day
            # grid every pair is same-day; only the rare different_day == 0 config
            # needs fresh per-day AND-flags here. When `same_day` is 0 the
            # `last_index · (1 − same_day)` slack drops the bound. C3/C5 give
            # `gap − 1 − min_gap >= 0`.
            same_day: cp_model.LinearExpr | int
            if not multi_day:
                same_day = 1
            elif aid in same_day_sum:
                same_day = same_day_sum[aid]
            else:
                gap_day_flags: list[cp_model.IntVar] = []
                for day in event_dates:
                    first = vars_by_choice_day.get((aid, 1, day), [])
                    second = vars_by_choice_day.get((aid, 2, day), [])
                    if not first or not second:
                        continue
                    on_day = model.new_bool_var(f"sdgap_on_{aid}_{day.isoformat()}")
                    model.add(on_day <= sum(first))
                    model.add(on_day <= sum(second))
                    model.add(on_day >= sum(first) + sum(second) - 1)
                    gap_day_flags.append(on_day)
                same_day = sum(gap_day_flags) if gap_day_flags else 0
            model.add(
                worst_same_day_gap
                >= gap - 1 - problem.min_gap_slots - last_index * (1 - same_day)
            )
        if worst_same_day_gap is not None:
            terms.append(weights.same_day_gap * worst_same_day_gap)

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

        # W_SUBDIVISION_SWITCH — keep each sub-division of a shared-panel
        # division in a contiguous block on a panel's day, so the same
        # interviewers are not flipped between Creative and WebMaster slot by
        # slot (Part 2). A switch is charged when two consecutive-on-the-grid,
        # same-day slots on one panel hold different sub-divisions. Only
        # divisions actually running >1 sub-division can incur it. Mirrors
        # objectives.count_subdivision_switches exactly.
        if score_subdivision_switch and weights.subdivision_switch:
            subdivision_of = {choice.key: choice.sub_division for choice in choices}
            subdivisions_by_division: dict[DivisionCode, set[str]] = defaultdict(set)
            for choice in choices:
                subdivisions_by_division[choice.division].add(choice.sub_division)

            # sum of x on (panel, slot) restricted to one sub-division — a
            # linear expression, no extra var (each is 0/1 by C2).
            by_panel_slot_subdivision: dict[tuple[str, str, str], list[cp_model.IntVar]] = (
                defaultdict(list)
            )
            for (applicant_id, choice_index, panel_id, slot_id), var in x.items():
                sub_division = subdivision_of[(applicant_id, choice_index)]
                by_panel_slot_subdivision[(panel_id, slot_id, sub_division)].append(var)

            ordered_slots = sorted(problem.slots, key=lambda s: s.slot_index)
            for panel in problem.panels:
                present = sorted(subdivisions_by_division.get(panel.division, set()))
                if len(present) < 2:
                    continue
                for index in range(len(ordered_slots) - 1):
                    earlier, later = ordered_slots[index], ordered_slots[index + 1]
                    if earlier.date != later.date or later.slot_index != earlier.slot_index + 1:
                        continue
                    # One switch var per (panel, adjacent pair). It is forced to
                    # 1 only when `earlier` holds one sub-division and `later`
                    # another (both 0/1 by C2, so at most one ordered pair can
                    # fire) — matching count_subdivision_switches' +1 per pair.
                    pair_constraints: list[tuple[list[cp_model.IntVar], list[cp_model.IntVar]]] = []
                    for here in present:
                        first_vars = by_panel_slot_subdivision.get(
                            (panel.id, earlier.slot_id, here), []
                        )
                        next_other = [
                            var
                            for other in present
                            if other != here
                            for var in by_panel_slot_subdivision.get(
                                (panel.id, later.slot_id, other), []
                            )
                        ]
                        if first_vars and next_other:
                            pair_constraints.append((first_vars, next_other))
                    if not pair_constraints:
                        continue
                    switch = model.new_bool_var(f"subdiv_switch_{panel.id}_{earlier.slot_id}")
                    for first_vars, next_other in pair_constraints:
                        model.add(sum(first_vars) + sum(next_other) - 1 <= switch)
                    terms.append(weights.subdivision_switch * switch)

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
