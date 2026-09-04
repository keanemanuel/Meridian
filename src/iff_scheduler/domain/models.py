"""Core domain entities (SPEC.md §6.3, data contracts; §7, repo architecture).

Plain pydantic models. No file handles, no path strings, no adapter types —
this is what lets the beta CSV/YAML -> Postgres swap happen without touching
these definitions (CLAUDE.md).
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime
from datetime import time as Time
from typing import Literal

from pydantic import BaseModel, ConfigDict

from iff_scheduler.domain.enums import DivisionCode, SendStatus, Severity

ChoiceIndex = Literal[1, 2]


class Slot(BaseModel):
    """An atomic, indivisible unit of time on the grid (slots.csv)."""

    model_config = ConfigDict(frozen=True)

    slot_id: str
    date: Date
    day_label: str
    start_time: Time
    end_time: Time
    slot_index: int


class Division(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: DivisionCode
    display: str


class Room(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    max_concurrent_panels: int
    divisions: list[DivisionCode]


class Panel(BaseModel):
    """One set of interviewers who can conduct one interview at a time (panels.csv)."""

    model_config = ConfigDict(frozen=True)

    id: str
    division: DivisionCode
    room: str
    active_slot_ids: list[str] = []


class Applicant(BaseModel):
    """A validated applicant record (applicants.clean.csv)."""

    model_config = ConfigDict(frozen=True)

    applicant_id: str
    full_name: str
    email: str
    phone: str
    sub_division_1: str
    sub_division_2: str
    division_1: DivisionCode
    division_2: DivisionCode
    availability_slots: list[str]
    submitted_at: datetime
    notes: str | None = None


class Interview(BaseModel):
    """One of an applicant's two choices, before it is placed on the grid."""

    model_config = ConfigDict(frozen=True)

    applicant_id: str
    choice_index: ChoiceIndex
    sub_division: str
    division: DivisionCode


class Assignment(BaseModel):
    """A concrete (applicant, choice, panel, slot) tuple in the schedule (assignments.csv)."""

    model_config = ConfigDict(frozen=True)

    applicant_id: str
    full_name: str
    email: str
    choice_index: ChoiceIndex
    sub_division: str
    division: DivisionCode
    panel_id: str
    room: str
    slot_id: str
    date: Date
    start_time: Time
    end_time: Time
    is_clash: bool
    is_locked: bool
    same_parent_pair: bool
    reason: str | None = None


class Conflict(BaseModel):
    """One row of the conflict report (conflicts.csv)."""

    model_config = ConfigDict(frozen=True)

    applicant_id: str
    severity: Severity
    type: str
    message: str


class SendLedgerEntry(BaseModel):
    """One row of the email send ledger, for idempotency (send_ledger.csv)."""

    model_config = ConfigDict(frozen=True)

    ledger_id: str
    applicant_id: str
    email: str
    template: str
    run_id: str
    status: SendStatus
    provider_message_id: str | None = None
    attempt_count: int = 0
    sent_at: datetime | None = None
    error: str | None = None


class Schedule(BaseModel):
    """One complete solve run's output."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    assignments: list[Assignment]
    conflicts: list[Conflict] = []
