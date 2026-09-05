"""Tests for M7 results — audit_result_recipients (SPEC.md §10.4, FR-65).

Mirrors test coverage the invite audit doesn't need to duplicate: blank
config next-step text is a batch-wide hard fail, and an ACCEPTED recipient
with no division_placed is caught even if `decide.py` somehow let one
through (defense in depth, same as `audit_invite_recipients`).
"""

from __future__ import annotations

from iff_scheduler.domain.enums import Decision
from iff_scheduler.notify.audit import audit_result_recipients
from iff_scheduler.results.decide import ResultRecipient
from iff_scheduler.settings import NotifyConfig

FULL_NOTIFY = NotifyConfig(
    next_steps_accepted="Reply to confirm.",
    next_steps_waitlist="We'll be in touch.",
    next_steps_rejected="Apply again next year.",
)


def _recipient(**overrides: object) -> ResultRecipient:
    base: dict[str, object] = dict(
        applicant_id="A001",
        full_name="Person",
        email="person@example.com",
        decision=Decision.REJECTED,
        division_placed_display=None,
        template_name="result_rejected",
    )
    base.update(overrides)
    return ResultRecipient(**base)  # type: ignore[arg-type]


def test_clean_batch_has_no_issues() -> None:
    assert audit_result_recipients([_recipient()], FULL_NOTIFY) == []


def test_blank_next_steps_config_is_a_batch_wide_issue() -> None:
    notify = NotifyConfig(next_steps_accepted="", next_steps_waitlist="x", next_steps_rejected="x")
    issues = audit_result_recipients([_recipient()], notify)
    assert any(i.code == "BLANK_CONFIG_FIELD" and i.applicant_id == "" for i in issues)


def test_invalid_email_is_rejected() -> None:
    issues = audit_result_recipients([_recipient(email="not-an-email")], FULL_NOTIFY)
    assert any(i.code == "INVALID_EMAIL" for i in issues)


def test_duplicate_email_across_two_recipients_is_flagged() -> None:
    recipients = [
        _recipient(applicant_id="A001", email="same@example.com"),
        _recipient(applicant_id="A002", email="same@example.com"),
    ]
    issues = audit_result_recipients(recipients, FULL_NOTIFY)
    assert any(i.code == "DUPLICATE_EMAIL" and i.applicant_id == "A002" for i in issues)


def test_accepted_without_division_placed_display_is_flagged() -> None:
    """Defense in depth: `decide.py` should never let this through, but the
    audit checks anyway, same as `audit_invite_recipients` re-checks
    completeness (CLAUDE.md invariant 3)."""
    recipient = _recipient(decision=Decision.ACCEPTED, division_placed_display=None)
    issues = audit_result_recipients([recipient], FULL_NOTIFY)
    assert any(i.code == "BLANK_FIELD" and "division_placed" in i.message for i in issues)


def test_accepted_with_division_placed_display_is_clean() -> None:
    recipient = _recipient(decision=Decision.ACCEPTED, division_placed_display="Creative")
    assert audit_result_recipients([recipient], FULL_NOTIFY) == []
