"""Applicant view: one row per applicant, both interviews (FR-51).

Built straight from `Assignment` rows — an assignment already carries
`full_name` and `email`, so no separate applicant list is needed here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as Date
from datetime import time as Time

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.models import Assignment, ChoiceIndex


@dataclass(frozen=True)
class ApplicantChoiceView:
    """One choice's placement, for the applicant view table."""

    division: DivisionCode
    sub_division: str
    panel_id: str
    room: str
    date: Date
    start_time: Time
    end_time: Time
    is_clash: bool
    is_locked: bool


@dataclass(frozen=True)
class ApplicantViewRow:
    """One applicant, both choices. Either choice may be missing — that is a
    C1 violation the run's conflicts.csv already reports (UNFILLED); the
    view renders it as a blank cell rather than guessing (CLAUDE.md
    invariant 3)."""

    applicant_id: str
    full_name: str
    email: str
    choice1: ApplicantChoiceView | None
    choice2: ApplicantChoiceView | None

    @property
    def has_clash(self) -> bool:
        return (self.choice1 is not None and self.choice1.is_clash) or (
            self.choice2 is not None and self.choice2.is_clash
        )


def _choice_view(a: Assignment | None) -> ApplicantChoiceView | None:
    if a is None:
        return None
    return ApplicantChoiceView(
        division=a.division,
        sub_division=a.sub_division,
        panel_id=a.panel_id,
        room=a.room,
        date=a.date,
        start_time=a.start_time,
        end_time=a.end_time,
        is_clash=a.is_clash,
        is_locked=a.is_locked,
    )


def build_applicant_view(assignments: Sequence[Assignment]) -> list[ApplicantViewRow]:
    """One row per applicant_id present in `assignments`, sorted for determinism."""
    by_applicant: dict[str, dict[ChoiceIndex, Assignment]] = defaultdict(dict)
    meta: dict[str, tuple[str, str]] = {}
    for a in assignments:
        by_applicant[a.applicant_id][a.choice_index] = a
        meta[a.applicant_id] = (a.full_name, a.email)

    rows: list[ApplicantViewRow] = []
    for applicant_id in sorted(by_applicant):
        choices = by_applicant[applicant_id]
        full_name, email = meta[applicant_id]
        rows.append(
            ApplicantViewRow(
                applicant_id=applicant_id,
                full_name=full_name,
                email=email,
                choice1=_choice_view(choices.get(1)),
                choice2=_choice_view(choices.get(2)),
            )
        )
    return rows
