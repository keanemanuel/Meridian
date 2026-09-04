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
from math import ceil
from typing import Literal

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import SlotGrid
from iff_scheduler.domain.models import Applicant
from iff_scheduler.settings import PanelEntry, PanelsConfig

Verdict = Literal["OK", "TIGHT", "INFEASIBLE"]


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


def _panel_active_slot_ids(panel: PanelEntry, grid: SlotGrid) -> set[str]:
    """A panel with no declared active_windows is active for the whole event (FR-25)."""
    if not panel.active_windows:
        return {slot.slot_id for slot in grid.slots}
    ids: set[str] = set()
    for slot in grid.slots:
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
    """Each applicant contributes one interview to each of their two choices'
    parent divisions — including twice to the same division for a same-parent
    pair (SPEC.md §1.2 Finding B, E-01)."""
    demand: Counter[DivisionCode] = Counter()
    for applicant in applicants:
        demand[applicant.division_1] += 1
        demand[applicant.division_2] += 1
    return demand


def compute_capacity_advisor(
    applicants: list[Applicant],
    panels: PanelsConfig,
    grid: SlotGrid,
    target_utilisation: float,
) -> list[DivisionCapacity]:
    """Build the per-division demand/supply table (SPEC.md §5.5)."""
    demand = _demand_by_division(applicants)

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

        active_by_panel = [_panel_active_slot_ids(panel, grid) for panel in division_panels]
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
