"""Config loading and schema validation.

Every tuneable value (SPEC.md §8) is validated against a pydantic model at
load time, so malformed config fails loudly here rather than deep inside the
solver (CLAUDE.md, "Conventions").
"""

from __future__ import annotations

from datetime import date as Date
from datetime import time as Time
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

from iff_scheduler.domain.enums import DivisionCode

# Repo root is three levels up from this file: src/iff_scheduler/settings.py
DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


class BreakWindow(BaseModel):
    """A window during which no slots are generated (FR-14)."""

    model_config = ConfigDict(frozen=True)

    start: Time
    end: Time


class DayConfig(BaseModel):
    """One event day: date, opening hours and optional breaks (FR-12, FR-13)."""

    model_config = ConfigDict(frozen=True)

    date: Date
    label: str
    start: Time
    end: Time
    breaks: list[BreakWindow] = []


class EventConfig(BaseModel):
    """config/event.yaml — dates, hours, interview duration, timezone (FR-10..16)."""

    model_config = ConfigDict(frozen=True)

    event_name: str
    timezone: str
    interview_duration_minutes: int
    min_gap_slots: int = 0
    # How a raw ticked availability block maps onto a grid slot when block size
    # and interview duration differ (FR-02, SPEC.md §9.2, E-05): "strict" requires
    # the slot to sit fully inside the applicant's ticked time; "lenient" counts a
    # slot covered once >=50% of it overlaps ticked time. Recommended: strict.
    availability_matching: Literal["strict", "lenient"] = "strict"
    days: list[DayConfig]


class DivisionEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: DivisionCode
    display: str


def canonical_sub_division(raw: str) -> str:
    """Fold a sub-division label to the form used for mapping lookups.

    Case, surrounding space and runs of internal whitespace are ignored, so
    "  finance and booth" and "Finance and  Booth" both resolve. Nothing else
    is touched: this is normalisation, not fuzzy matching, so an unlisted
    value still fails loudly (FR-03, CLAUDE.md invariant 3).
    """
    return " ".join((raw or "").split()).lower()


class DivisionsConfig(BaseModel):
    """config/divisions.yaml — parent divisions and the sub-division mapping (FR-03)."""

    model_config = ConfigDict(frozen=True)

    divisions: list[DivisionEntry]
    sub_division_mapping: dict[str, DivisionCode]

    @property
    def canonical_sub_division_mapping(self) -> dict[str, DivisionCode]:
        """`sub_division_mapping` re-keyed by `canonical_sub_division`.

        Google Forms preserves the option text it was authored with, including
        double spaces and stray capitals; matching on the raw string alone
        rejected valid registrants whose answer differed only in whitespace.
        """
        return {
            canonical_sub_division(name): code for name, code in self.sub_division_mapping.items()
        }

    @model_validator(mode="after")
    def _mapping_has_no_canonical_collisions(self) -> DivisionsConfig:
        """Two keys that fold to the same canonical form but point at different
        divisions would make the lookup order-dependent. Fail at load time
        (CLAUDE.md: "fail loudly on malformed config")."""
        seen: dict[str, DivisionCode] = {}
        for name, code in self.sub_division_mapping.items():
            key = canonical_sub_division(name)
            if key in seen and seen[key] != code:
                raise ValueError(
                    f"sub_division_mapping: '{name}' collides with an earlier entry "
                    f"that maps to {seen[key].value}, not {code.value}."
                )
            seen[key] = code
        return self


class RoomEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    max_concurrent_panels: int
    divisions: list[DivisionCode]
    # Which event days this physical room is available on (FR-20). Empty means
    # every day. A room listed for Thursday only cannot host a Friday slot, and
    # `resolve_panels` shrinks any panel in it to that day's slots accordingly.
    days: list[Date] = []


class RoomsConfig(BaseModel):
    """config/rooms.yaml — rooms, capacity and division->room mapping (FR-20, FR-21)."""

    model_config = ConfigDict(frozen=True)

    rooms: list[RoomEntry]


class ActiveWindow(BaseModel):
    model_config = ConfigDict(frozen=True)

    date: Date
    start: Time
    end: Time


class PanelEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    division: DivisionCode
    room: str
    active_windows: list[ActiveWindow] = []


class PanelsConfig(BaseModel):
    """config/panels.yaml — interviewer panels, the parallelism knob (FR-22)."""

    model_config = ConfigDict(frozen=True)

    panels: list[PanelEntry]


class SolverWeights(BaseModel):
    model_config = ConfigDict(frozen=True)

    clash: int
    repeat_panel: int
    spread: int
    balance: int
    lateness: int


class SolverConfig(BaseModel):
    """config/solver.yaml — solver choice, weights, gap, time limit, seed."""

    model_config = ConfigDict(frozen=True)

    solver: str
    random_seed: int
    time_limit_seconds: int
    two_phase: bool = True
    phase1_time_fraction: float = 0.5
    weights: SolverWeights
    applicant_cap: int
    target_utilisation: float


class NotifyConfig(BaseModel):
    """config/notify.yaml — sender identity, throttle, template map, and the
    invite content SPEC.md §10.3 requires (arrival instructions, what to
    bring, contact, RSVP deadline) — kept in config, not templates, so none
    of it is a magic string in `src/` (CLAUDE.md invariant 5)."""

    model_config = ConfigDict(frozen=True)

    sender_name: str = ""
    sender_email: str = ""
    reply_to: str = ""
    throttle_seconds: float = 1.0
    templates: dict[str, str] = {}
    contact_name: str = ""
    contact_channel: str = ""
    rsvp_deadline: str = ""
    arrival_minutes_early: int = 15
    what_to_bring: list[str] = []

    # Result email content (FR-65, SPEC.md §10.4) — kept in config, not
    # templates, so the committee's per-decision message isn't a magic
    # string in `src/` (CLAUDE.md invariant 5). Each next_steps_* value must
    # be non-blank before a send is allowed (audit_result_recipients).
    result_deadline: str = ""
    next_steps_accepted: str = ""
    next_steps_waitlist: str = ""
    next_steps_rejected: str = ""


class Settings(BaseModel):
    """All configuration for one run, aggregated and validated."""

    model_config = ConfigDict(frozen=True)

    event: EventConfig
    divisions: DivisionsConfig
    rooms: RoomsConfig
    panels: PanelsConfig
    solver: SolverConfig
    notify: NotifyConfig


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Config file is empty or not a mapping: {path}")
    return data


def load_event_config(path: Path) -> EventConfig:
    return EventConfig.model_validate(_load_yaml(path))


def load_divisions_config(path: Path) -> DivisionsConfig:
    return DivisionsConfig.model_validate(_load_yaml(path))


def load_rooms_config(path: Path) -> RoomsConfig:
    return RoomsConfig.model_validate(_load_yaml(path))


def load_panels_config(path: Path) -> PanelsConfig:
    return PanelsConfig.model_validate(_load_yaml(path))


def load_solver_config(path: Path) -> SolverConfig:
    return SolverConfig.model_validate(_load_yaml(path))


def load_notify_config(path: Path) -> NotifyConfig:
    return NotifyConfig.model_validate(_load_yaml(path))


def load_settings(config_dir: Path = DEFAULT_CONFIG_DIR) -> Settings:
    """Load and validate every config file in `config_dir` into one Settings object."""
    return Settings(
        event=load_event_config(config_dir / "event.yaml"),
        divisions=load_divisions_config(config_dir / "divisions.yaml"),
        rooms=load_rooms_config(config_dir / "rooms.yaml"),
        panels=load_panels_config(config_dir / "panels.yaml"),
        solver=load_solver_config(config_dir / "solver.yaml"),
        notify=load_notify_config(config_dir / "notify.yaml"),
    )
