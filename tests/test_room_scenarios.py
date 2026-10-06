"""Tests for the room-scenario mechanism (SPEC.md §3.3/§5.5): a selectable,
alternate room layout ("extended_waiting_rooms" — 3013 and 3033 additionally
converted to waiting rooms) that sits alongside the committed default without
touching it.

Full verification against the real ~108-applicant dataset (feasibility,
panel distribution, room-exclusivity rate) is written up in
`docs/room_scenario_extended_waiting_rooms.md`; these tests cover the
mechanism (config resolution, config validity, non-regression of the
default) and a small synthetic solve so the scenario has CI coverage without
needing that real, gitignored dataset.
"""

from __future__ import annotations

from datetime import date

import pytest
from tests.test_feasibility import _applicant

from iff_scheduler.domain.grid import build_slot_grid
from iff_scheduler.scheduling.base import (
    USABLE_STATUSES,
    resolve_panels,
    resolve_rooms,
    validate_problem,
)
from iff_scheduler.scheduling.solver_cpsat import CpSatSolver
from iff_scheduler.settings import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_ROOM_SCENARIO,
    Settings,
    known_room_scenarios,
    load_settings,
    resolve_room_scenario_dir,
)

EXTENDED = "extended_waiting_rooms"


# ---------------------------------------------------------------------------
# Resolution mechanism
# ---------------------------------------------------------------------------


def test_default_scenario_name_resolves_to_the_committed_config_dir() -> None:
    """ "default"/None must mean "use config_dir exactly as committed" — the
    existing default room setup stays reachable exactly as it works today."""
    assert resolve_room_scenario_dir(DEFAULT_CONFIG_DIR, None) == DEFAULT_CONFIG_DIR
    assert (
        resolve_room_scenario_dir(DEFAULT_CONFIG_DIR, DEFAULT_ROOM_SCENARIO) == DEFAULT_CONFIG_DIR
    )


def test_extended_waiting_rooms_scenario_resolves_under_scenarios_dir() -> None:
    resolved = resolve_room_scenario_dir(DEFAULT_CONFIG_DIR, EXTENDED)
    assert resolved == DEFAULT_CONFIG_DIR / "scenarios" / EXTENDED
    assert (resolved / "rooms.yaml").exists()


def test_unknown_room_scenario_fails_loudly() -> None:
    """CLAUDE.md invariant 3: an unrecognised choice is rejected, never
    silently substituted with the default."""
    with pytest.raises(ValueError, match="Unknown room scenario"):
        resolve_room_scenario_dir(DEFAULT_CONFIG_DIR, "made_up_scenario")


def test_known_room_scenarios_lists_default_and_extended() -> None:
    names = known_room_scenarios(DEFAULT_CONFIG_DIR)
    assert names[0] == DEFAULT_ROOM_SCENARIO
    assert EXTENDED in names


# ---------------------------------------------------------------------------
# The default scenario is provably untouched
# ---------------------------------------------------------------------------


def test_default_settings_load_identically_regardless_of_scenario_helper() -> None:
    """Loading via the new helper with "default" must be byte-for-byte the
    settings the app already loads today — additive, not a regression."""
    direct = load_settings(DEFAULT_CONFIG_DIR)
    via_helper = load_settings(resolve_room_scenario_dir(DEFAULT_CONFIG_DIR, DEFAULT_ROOM_SCENARIO))
    assert direct == via_helper
    assert direct.rooms == via_helper.rooms
    assert direct.panels == via_helper.panels


def test_default_room_layout_is_unchanged() -> None:
    """The non-negotiable baseline this feature must not disturb: only 2018
    is a waiting room; Thursday has 5 interview rooms, Friday 4."""
    settings = load_settings(DEFAULT_CONFIG_DIR)
    interview_rooms = [r for r in settings.rooms.rooms if r.interview_room]
    waiting_rooms = [r for r in settings.rooms.rooms if not r.interview_room]
    assert {r.id for r in waiting_rooms} == {"2018"}

    thursday = date(2026, 9, 17)
    friday = date(2026, 9, 18)
    thu_rooms = [r for r in interview_rooms if not r.days or thursday in r.days]
    fri_rooms = [r for r in interview_rooms if not r.days or friday in r.days]
    assert len(thu_rooms) == 5
    assert len(fri_rooms) == 4


# ---------------------------------------------------------------------------
# The extended_waiting_rooms scenario's own config
# ---------------------------------------------------------------------------


@pytest.fixture()
def extended_settings() -> Settings:
    return load_settings(resolve_room_scenario_dir(DEFAULT_CONFIG_DIR, EXTENDED))


def test_extended_scenario_withdraws_exactly_2018_3013_3033(
    extended_settings: Settings,
) -> None:
    waiting_rooms = {r.id for r in extended_settings.rooms.rooms if not r.interview_room}
    assert waiting_rooms == {"2018", "3013", "3033"}
    # A waiting room must carry no divisions (settings.py's own invariant);
    # re-asserted here so a future edit that breaks it fails in this suite.
    for r in extended_settings.rooms.rooms:
        if not r.interview_room:
            assert r.divisions == []


def test_extended_scenario_room_counts_match_the_spec(extended_settings: Settings) -> None:
    """Thursday 4 rooms (down from 5), Friday 3 (down from 4) — exactly the
    counts requested."""
    interview_rooms = [r for r in extended_settings.rooms.rooms if r.interview_room]
    thursday = date(2026, 9, 17)
    friday = date(2026, 9, 18)
    thu_rooms = [r for r in interview_rooms if not r.days or thursday in r.days]
    fri_rooms = [r for r in interview_rooms if not r.days or friday in r.days]
    assert len(thu_rooms) == 4
    assert len(fri_rooms) == 3


def test_extended_scenario_stays_within_the_hard_concurrency_ceiling(
    extended_settings: Settings,
) -> None:
    """The scenario is allowed to raise `max_concurrent_panels` per room, but
    never above ROOM_CONCURRENCY_CEILING — and per the measured verification
    (docs/room_scenario_extended_waiting_rooms.md) it did not need to raise
    it at all: 5 was sufficient for the real dataset."""
    from iff_scheduler.settings import ROOM_CONCURRENCY_CEILING

    for r in extended_settings.rooms.rooms:
        assert r.max_concurrent_panels <= ROOM_CONCURRENCY_CEILING


def test_extended_scenario_baseline_panels_reference_valid_rooms(
    extended_settings: Settings,
) -> None:
    """Every baseline panel must sit in a room that (a) is still an interview
    room and (b) is configured for that panel's division — otherwise
    `validate_problem` rejects the problem deep inside the solver instead of
    here (CLAUDE.md: fail loudly at load time)."""
    rooms_by_id = {r.id: r for r in extended_settings.rooms.rooms}
    for panel in extended_settings.panels.panels:
        room = rooms_by_id[panel.room]
        assert room.interview_room, f"{panel.id} sits in waiting room {room.id}"
        assert panel.division in room.divisions


def test_extended_scenario_shares_non_room_config_with_default() -> None:
    """The scenario must differ from the default ONLY in rooms/panels — event
    timing, divisions, solver weights and notify content all being identical
    is what makes this an additive room-layout experiment rather than a
    second, drifting copy of the whole config."""
    default = load_settings(DEFAULT_CONFIG_DIR)
    extended = load_settings(resolve_room_scenario_dir(DEFAULT_CONFIG_DIR, EXTENDED))
    assert default.event == extended.event
    assert default.divisions == extended.divisions
    assert default.solver == extended.solver
    assert default.notify == extended.notify


# ---------------------------------------------------------------------------
# A small synthetic solve under the extended scenario (fast CI coverage;
# the real-data solve lives in docs/room_scenario_extended_waiting_rooms.md)
# ---------------------------------------------------------------------------


def test_extended_scenario_solves_a_small_synthetic_instance(
    extended_settings: Settings,
) -> None:
    from iff_scheduler.domain.enums import DivisionCode

    grid = build_slot_grid(extended_settings.event)
    all_slots = [s.slot_id for s in grid.slots]
    applicants = [
        _applicant(f"A{i}", DivisionCode.MEDMARDOC, DivisionCode.CREATIVE, all_slots)
        for i in range(6)
    ]

    panels = resolve_panels(extended_settings.panels, extended_settings.rooms, grid)
    rooms = resolve_rooms(extended_settings.rooms, grid)
    from iff_scheduler.scheduling.base import SolveProblem

    problem = SolveProblem(
        applicants=applicants,
        panels=panels,
        rooms=rooms,
        slots=grid.slots,
        weights=extended_settings.solver.weights,
        min_gap_slots=extended_settings.event.min_gap_slots,
        locks=[],
        two_phase=extended_settings.solver.two_phase,
        time_limit_seconds=10.0,
        phase1_time_fraction=extended_settings.solver.phase1_time_fraction,
        random_seed=extended_settings.solver.random_seed,
    )
    validate_problem(problem)  # every panel's room is real and correctly configured

    result = CpSatSolver().solve(problem)
    assert result.status in USABLE_STATUSES
    assert len(result.assignments) == len(applicants) * 2
