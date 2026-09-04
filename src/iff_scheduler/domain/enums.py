"""Shared enumerations for the scheduler core."""

from __future__ import annotations

from enum import StrEnum


class DivisionCode(StrEnum):
    """The six parent divisions the solver operates on (SPEC.md §2)."""

    MEDMARDOC = "MEDMARDOC"
    CREATIVE = "CREATIVE"
    LOGISTICS = "LOGISTICS"
    LIAISON = "LIAISON"
    FNB = "FNB"
    PROGRAM = "PROGRAM"


class Severity(StrEnum):
    """Conflict report severity (SPEC.md §6.3, conflicts.csv)."""

    RED = "RED"
    AMBER = "AMBER"


class SendStatus(StrEnum):
    """Email send ledger status (SPEC.md §6.3, send_ledger.csv)."""

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
