"""Send ledger — idempotency and retry state (FR-62, SPEC.md §6.3
send_ledger.csv, §10.2 steps 5-6).

Pure logic over `SendLedgerEntry` rows: reading/writing the CSV itself is
the CLI's job, the same split `review/locks.py` uses for pinned assignments
(parsing here, pandas at the edge).

The ledger is keyed by `(applicant_id, template)`, not by run — "a re-run
never double-sends" (FR-62) means never double-sending the same applicant
the same email, regardless of which solve run produced the invite. A retry
replaces the stale row for that key rather than appending a duplicate, so
`attempt_count` accumulates correctly across FAILED -> SENT transitions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime

from iff_scheduler.domain.enums import SendStatus
from iff_scheduler.domain.models import SendLedgerEntry

LEDGER_COLUMNS = [
    "ledger_id",
    "applicant_id",
    "email",
    "template",
    "run_id",
    "status",
    "provider_message_id",
    "attempt_count",
    "sent_at",
    "error",
]


def make_ledger_id(applicant_id: str, template: str) -> str:
    """The ledger key: one row per applicant per email template, ever."""
    return f"{applicant_id}:{template}"


def parse_ledger_rows(rows: Iterable[Mapping[str, str]]) -> list[SendLedgerEntry]:
    """Parse already-read CSV rows (dicts of strings) into `SendLedgerEntry`."""
    entries: list[SendLedgerEntry] = []
    for row in rows:
        missing = [c for c in LEDGER_COLUMNS if c not in row]
        if missing:
            raise ValueError(f"Ledger row is missing column(s) {missing}: {dict(row)}")
        raw_attempt = row["attempt_count"]
        try:
            attempt_count = int(raw_attempt) if raw_attempt else 0
        except ValueError as exc:
            raise ValueError(
                f"attempt_count must be an integer, got {raw_attempt!r} "
                f"for ledger row {row['ledger_id']!r}"
            ) from exc
        entries.append(
            SendLedgerEntry(
                ledger_id=row["ledger_id"],
                applicant_id=row["applicant_id"],
                email=row["email"],
                template=row["template"],
                run_id=row["run_id"],
                status=SendStatus(row["status"]),
                provider_message_id=row["provider_message_id"] or None,
                attempt_count=attempt_count,
                sent_at=datetime.fromisoformat(row["sent_at"]) if row["sent_at"] else None,
                error=row["error"] or None,
            )
        )
    return entries


def ledger_entry_to_row(entry: SendLedgerEntry) -> dict[str, str]:
    return {
        "ledger_id": entry.ledger_id,
        "applicant_id": entry.applicant_id,
        "email": entry.email,
        "template": entry.template,
        "run_id": entry.run_id,
        "status": entry.status.value,
        "provider_message_id": entry.provider_message_id or "",
        "attempt_count": str(entry.attempt_count),
        "sent_at": entry.sent_at.isoformat() if entry.sent_at else "",
        "error": entry.error or "",
    }


def already_sent(ledger: Sequence[SendLedgerEntry], applicant_id: str, template: str) -> bool:
    """True if this applicant already has a SENT row for this template — the
    idempotency check that makes a re-run skip everyone already sent to
    after a crash mid-batch (SPEC.md §10.2 step 5)."""
    ledger_id = make_ledger_id(applicant_id, template)
    entry = next((e for e in ledger if e.ledger_id == ledger_id), None)
    return entry is not None and entry.status == SendStatus.SENT


def record_attempt(
    ledger: Sequence[SendLedgerEntry],
    *,
    applicant_id: str,
    email: str,
    template: str,
    run_id: str,
    status: SendStatus,
    provider_message_id: str | None = None,
    sent_at: datetime | None = None,
    error: str | None = None,
) -> list[SendLedgerEntry]:
    """Return a new ledger with one attempt recorded for `(applicant_id,
    template)`. Replaces any existing row for that key and carries its
    `attempt_count` forward, so a FAILED row followed by a retry's SENT row
    ends up as a single row with `attempt_count == 2`, not two rows."""
    ledger_id = make_ledger_id(applicant_id, template)
    existing = next((e for e in ledger if e.ledger_id == ledger_id), None)
    entry = SendLedgerEntry(
        ledger_id=ledger_id,
        applicant_id=applicant_id,
        email=email,
        template=template,
        run_id=run_id,
        status=status,
        provider_message_id=provider_message_id,
        attempt_count=(existing.attempt_count if existing is not None else 0) + 1,
        sent_at=sent_at,
        error=error,
    )
    return [*(e for e in ledger if e.ledger_id != ledger_id), entry]
