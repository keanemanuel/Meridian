"""Tests for M7 results — decide.py (SPEC.md §10.4, §13 M7).

The completeness rule is the one that matters: every applicant in the
roster must resolve to exactly one recipient, and a missing/duplicate
decision is reported rather than silently dropped or defaulted
(CLAUDE.md invariant 3).
"""

from __future__ import annotations

from datetime import datetime

from iff_scheduler.domain.enums import Decision, DivisionCode
from iff_scheduler.domain.models import Applicant, DecisionRecord
from iff_scheduler.results.decide import build_result_recipients, partition_by_decision
from iff_scheduler.settings import DivisionEntry, DivisionsConfig

DIVISIONS = DivisionsConfig(
    divisions=[
        DivisionEntry(code=DivisionCode.CREATIVE, display="Creative"),
        DivisionEntry(code=DivisionCode.LOGISTICS, display="Logistics"),
    ],
    sub_division_mapping={"Creative": DivisionCode.CREATIVE, "Logistics": DivisionCode.LOGISTICS},
)


def _applicant(applicant_id: str, full_name: str = "Person", email: str | None = None) -> Applicant:
    return Applicant(
        applicant_id=applicant_id,
        full_name=full_name,
        email=email or f"{applicant_id.lower()}@example.com",
        phone="+62",
        sub_division_1="Creative",
        sub_division_2="Logistics",
        division_1=DivisionCode.CREATIVE,
        division_2=DivisionCode.LOGISTICS,
        availability_slots=["2026-09-17_1800"],
        submitted_at=datetime(2026, 8, 1),
        notes=None,
    )


def test_every_applicant_gets_a_recipient_when_decisions_are_complete() -> None:
    applicants = [_applicant("A001"), _applicant("A002")]
    decisions = [
        DecisionRecord(
            applicant_id="A001", decision=Decision.ACCEPTED, division_placed=DivisionCode.CREATIVE
        ),
        DecisionRecord(applicant_id="A002", decision=Decision.REJECTED),
    ]

    recipients, issues = build_result_recipients(applicants, decisions, DIVISIONS)

    assert issues == []
    assert {r.applicant_id: r.decision for r in recipients} == {
        "A001": Decision.ACCEPTED,
        "A002": Decision.REJECTED,
    }
    accepted = next(r for r in recipients if r.applicant_id == "A001")
    assert accepted.division_placed_display == "Creative"
    assert accepted.template_name == "result_accepted"


def test_missing_decision_is_a_hard_issue_not_a_default_rejection() -> None:
    """SPEC.md §10.4: a blank decision must be a hard failure, never a
    default to 'rejected' — an applicant absent from the scores sheet must
    surface as MISSING_DECISION, not silently become REJECTED."""
    applicants = [_applicant("A001")]
    recipients, issues = build_result_recipients(applicants, [], DIVISIONS)

    assert recipients == []
    assert len(issues) == 1
    assert issues[0].code == "MISSING_DECISION"
    assert issues[0].applicant_id == "A001"


def test_duplicate_decision_for_one_applicant_is_reported() -> None:
    applicants = [_applicant("A001")]
    decisions = [
        DecisionRecord(
            applicant_id="A001", decision=Decision.ACCEPTED, division_placed=DivisionCode.CREATIVE
        ),
        DecisionRecord(applicant_id="A001", decision=Decision.REJECTED),
    ]

    recipients, issues = build_result_recipients(applicants, decisions, DIVISIONS)

    assert recipients == []
    assert any(i.code == "DUPLICATE_DECISION" for i in issues)


def test_accepted_without_division_placed_is_reported() -> None:
    applicants = [_applicant("A001")]
    decisions = [
        DecisionRecord(applicant_id="A001", decision=Decision.ACCEPTED, division_placed=None)
    ]

    recipients, issues = build_result_recipients(applicants, decisions, DIVISIONS)

    assert recipients == []
    assert any(i.code == "MISSING_DIVISION_PLACED" for i in issues)


def test_decision_for_unknown_applicant_is_reported() -> None:
    applicants = [_applicant("A001")]
    decisions = [
        DecisionRecord(applicant_id="A001", decision=Decision.REJECTED),
        DecisionRecord(applicant_id="ZZZ", decision=Decision.REJECTED),
    ]

    recipients, issues = build_result_recipients(applicants, decisions, DIVISIONS)

    assert len(recipients) == 1
    assert any(i.code == "UNKNOWN_APPLICANT" and i.applicant_id == "ZZZ" for i in issues)


def test_partition_by_decision_groups_and_includes_empty_groups() -> None:
    applicants = [_applicant("A001"), _applicant("A002")]
    decisions = [
        DecisionRecord(
            applicant_id="A001", decision=Decision.ACCEPTED, division_placed=DivisionCode.CREATIVE
        ),
        DecisionRecord(
            applicant_id="A002", decision=Decision.ACCEPTED, division_placed=DivisionCode.CREATIVE
        ),
    ]
    recipients, issues = build_result_recipients(applicants, decisions, DIVISIONS)
    assert issues == []

    grouped = partition_by_decision(recipients)

    assert len(grouped[Decision.ACCEPTED]) == 2
    assert grouped[Decision.WAITLIST] == []
    assert grouped[Decision.REJECTED] == []
