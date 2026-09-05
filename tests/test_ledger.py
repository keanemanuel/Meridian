"""Tests for M6 notify — the send ledger (SPEC.md §3.7 FR-62, §6.3
send_ledger.csv, §10.2 steps 5-6).

Two things must hold:

1. Parsing and serialising a ledger row round-trip (mirrors test_locks.py's
   coverage of locks.py).
2. `already_sent` + `record_attempt` together make a re-run idempotent: once
   an applicant has a SENT row for a template, a second attempt is skipped,
   never double-recorded (CLAUDE.md invariant 6: "Emails are idempotent").
"""

from __future__ import annotations

from datetime import datetime

import pytest

from iff_scheduler.domain.enums import SendStatus
from iff_scheduler.domain.models import SendLedgerEntry
from iff_scheduler.notify.ledger import (
    already_sent,
    ledger_entry_to_row,
    make_ledger_id,
    parse_ledger_rows,
    record_attempt,
)

SENT_AT = datetime(2026, 9, 1, 12, 0, 0)


def make_entry(
    applicant_id: str = "A1",
    template: str = "invite",
    status: SendStatus = SendStatus.SENT,
    attempt_count: int = 1,
    run_id: str = "2026-09-01T12-00-00",
    provider_message_id: str | None = "msg-1",
    sent_at: datetime | None = SENT_AT,
    error: str | None = None,
) -> SendLedgerEntry:
    return SendLedgerEntry(
        ledger_id=make_ledger_id(applicant_id, template),
        applicant_id=applicant_id,
        email=f"{applicant_id.lower()}@example.com",
        template=template,
        run_id=run_id,
        status=status,
        provider_message_id=provider_message_id,
        attempt_count=attempt_count,
        sent_at=sent_at,
        error=error,
    )


# ------------------------------------------------------------- parse / round-trip


def test_ledger_entry_to_row_round_trips_with_parse_ledger_rows() -> None:
    entry = make_entry()

    parsed = parse_ledger_rows([ledger_entry_to_row(entry)])

    assert parsed == [entry]


def test_parse_ledger_rows_round_trips_a_pending_entry_with_blank_fields() -> None:
    """A PENDING/FAILED row before any attempt has no provider id, sent_at or
    error — the round trip must preserve those as None, not the string
    "None" or "nan" (CLAUDE.md invariant 3: "Nothing is guessed")."""
    entry = make_entry(
        status=SendStatus.FAILED,
        provider_message_id=None,
        sent_at=None,
        error="SMTP timeout",
    )

    parsed = parse_ledger_rows([ledger_entry_to_row(entry)])

    assert parsed == [entry]
    assert parsed[0].provider_message_id is None
    assert parsed[0].sent_at is None


def test_parse_ledger_rows_rejects_missing_column() -> None:
    with pytest.raises(ValueError, match="missing column"):
        parse_ledger_rows([{"applicant_id": "A1", "template": "invite"}])


def test_parse_ledger_rows_rejects_non_integer_attempt_count() -> None:
    row = ledger_entry_to_row(make_entry())
    row["attempt_count"] = "many"
    with pytest.raises(ValueError, match="integer"):
        parse_ledger_rows([row])


def test_make_ledger_id_is_keyed_by_applicant_and_template_not_run() -> None:
    """FR-62: idempotency must hold across solve runs, not just within one —
    the same applicant/template pair always maps to the same ledger row."""
    assert make_ledger_id("A1", "invite") == make_ledger_id("A1", "invite")
    assert make_ledger_id("A1", "invite") != make_ledger_id("A1", "result_accepted")
    assert make_ledger_id("A1", "invite") != make_ledger_id("A2", "invite")


# ------------------------------------------------------------ already_sent


def test_already_sent_is_false_for_an_empty_ledger() -> None:
    assert already_sent([], "A1", "invite") is False


def test_already_sent_is_true_only_for_a_sent_status() -> None:
    ledger = [make_entry("A1", status=SendStatus.SENT)]
    assert already_sent(ledger, "A1", "invite") is True


@pytest.mark.parametrize("status", [SendStatus.PENDING, SendStatus.FAILED, SendStatus.SKIPPED])
def test_already_sent_is_false_for_non_sent_statuses(status: SendStatus) -> None:
    """A FAILED or PENDING row must not block a retry — only SENT does
    (SPEC.md §10.2 step 6, FR-66)."""
    ledger = [make_entry("A1", status=status)]
    assert already_sent(ledger, "A1", "invite") is False


def test_already_sent_does_not_cross_templates() -> None:
    """An invite SENT must not make the result email look already sent."""
    ledger = [make_entry("A1", template="invite", status=SendStatus.SENT)]
    assert already_sent(ledger, "A1", "result_accepted") is False


# ----------------------------------------------------------- record_attempt


def test_record_attempt_appends_a_new_row_for_a_first_attempt() -> None:
    ledger = record_attempt(
        [],
        applicant_id="A1",
        email="a1@example.com",
        template="invite",
        run_id="run-1",
        status=SendStatus.SENT,
        provider_message_id="msg-1",
        sent_at=SENT_AT,
    )

    assert len(ledger) == 1
    assert ledger[0].applicant_id == "A1"
    assert ledger[0].status == SendStatus.SENT
    assert ledger[0].attempt_count == 1


def test_record_attempt_replaces_the_stale_row_and_carries_attempt_count_forward() -> None:
    """A FAILED attempt followed by a successful retry must end up as ONE row
    with attempt_count == 2, not two separate rows — otherwise a naive
    'append' ledger would let a crash-and-retry look like two sends when a
    human reads it (FR-62)."""
    ledger = [make_entry("A1", status=SendStatus.FAILED, attempt_count=1, error="timeout")]

    ledger = record_attempt(
        ledger,
        applicant_id="A1",
        email="a1@example.com",
        template="invite",
        run_id="run-2",
        status=SendStatus.SENT,
        provider_message_id="msg-2",
        sent_at=SENT_AT,
    )

    assert len(ledger) == 1
    assert ledger[0].status == SendStatus.SENT
    assert ledger[0].attempt_count == 2
    assert ledger[0].error is None


def test_record_attempt_leaves_other_applicants_untouched() -> None:
    ledger = [make_entry("A1"), make_entry("A2")]

    ledger = record_attempt(
        ledger,
        applicant_id="A1",
        email="a1@example.com",
        template="invite",
        run_id="run-2",
        status=SendStatus.SENT,
    )

    ids = {e.applicant_id for e in ledger}
    assert ids == {"A1", "A2"}
    assert len(ledger) == 2


# --------------------------------------------------- end-to-end idempotency


def test_a_second_send_pass_skips_everyone_already_sent() -> None:
    """The scenario SPEC.md §10.2's ledger section describes directly: a
    crash after recipient 1 of 2, then a re-run must send only the second
    (CLAUDE.md invariant 6: "A re-run must never double-send")."""
    ledger: list[SendLedgerEntry] = []
    recipients = ["A1", "A2"]

    # First pass: A1 succeeds, A2 "crashes" (never attempted).
    ledger = record_attempt(
        ledger,
        applicant_id="A1",
        email="a1@example.com",
        template="invite",
        run_id="run-1",
        status=SendStatus.SENT,
        provider_message_id="msg-1",
        sent_at=SENT_AT,
    )

    pending_after_crash = [r for r in recipients if not already_sent(ledger, r, "invite")]
    assert pending_after_crash == ["A2"]

    # Re-run: only A2 gets attempted.
    ledger = record_attempt(
        ledger,
        applicant_id="A2",
        email="a2@example.com",
        template="invite",
        run_id="run-2",
        status=SendStatus.SENT,
        provider_message_id="msg-2",
        sent_at=SENT_AT,
    )

    assert all(already_sent(ledger, r, "invite") for r in recipients)
    assert len(ledger) == 2
