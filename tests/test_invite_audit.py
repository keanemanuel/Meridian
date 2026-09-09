"""Tests for the invite completeness audit (FR-64, SPEC.md §10.2).

A normal applicant is owed two interviews; a single-choice applicant — one
role, or the same role twice (E-01b) — is owed one. `audit_invite_recipients`
must flag the first kind when an interview is missing and leave the second
kind alone.
"""

from __future__ import annotations

from datetime import date, time

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.models import Assignment
from iff_scheduler.notify.audit import audit_invite_recipients
from iff_scheduler.notify.renderer import build_invite_recipients
from iff_scheduler.settings import load_settings


def _assignment(applicant_id: str, choice_index: int, *, name: str = "Person") -> Assignment:
    return Assignment(
        applicant_id=applicant_id,
        full_name=name,
        email=f"{applicant_id.lower()}@example.com",
        choice_index=1 if choice_index == 1 else 2,
        sub_division="Program" if choice_index == 1 else "Logistics",
        division=DivisionCode.PROGRAM if choice_index == 1 else DivisionCode.LOGISTICS,
        panel_id=f"PANEL-{choice_index}",
        room="R1",
        slot_id=f"2026-09-17_18{choice_index}0",
        date=date(2026, 9, 17),
        start_time=time(18 + choice_index, 0),
        end_time=time(18 + choice_index, 20),
        is_clash=False,
        is_locked=False,
        same_parent_pair=False,
    )


def _recipients(assignments, single_choice_ids=()):
    settings = load_settings()
    return build_invite_recipients(
        assignments, settings.divisions, settings.event, single_choice_ids=single_choice_ids
    )


def test_two_choice_applicant_with_both_interviews_passes() -> None:
    recipients = _recipients([_assignment("A001", 1), _assignment("A001", 2)])
    assert audit_invite_recipients(recipients) == []


def test_two_choice_applicant_missing_second_interview_is_flagged() -> None:
    recipients = _recipients([_assignment("A001", 1)])
    issues = audit_invite_recipients(recipients)
    assert any(i.code == "MISSING_ASSIGNMENT" and i.applicant_id == "A001" for i in issues)


def test_single_choice_applicant_with_one_interview_passes() -> None:
    """E-01b / single-choice rows: owed one interview, so one is complete."""
    recipients = _recipients([_assignment("A001", 1)], single_choice_ids={"A001"})
    assert recipients[0].interviews_owed == 1
    assert recipients[0].is_complete is True
    assert audit_invite_recipients(recipients) == []


def test_single_choice_applicant_with_no_interview_is_still_flagged() -> None:
    recipients = _recipients([_assignment("A002", 1)], single_choice_ids={"A001"})
    # A001 has no assignment at all, so it never reaches build_invite_recipients;
    # A002 is owed two and only has one.
    issues = audit_invite_recipients(recipients)
    assert any(i.code == "MISSING_ASSIGNMENT" and i.applicant_id == "A002" for i in issues)
