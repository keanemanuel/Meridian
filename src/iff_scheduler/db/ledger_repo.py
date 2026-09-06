"""CRUD for the `send_ledger` table (beta replacement for send_ledger.csv).

FR-62 — "a re-run never double-sends" — is keyed by `(applicant_id,
template)` regardless of which solve run produced the invite. The unique
index in 001_initial.sql is `(run_id, applicant_id, template)`; cross-run
idempotency is enforced the same way the CSV store does it, by
`iff_scheduler.notify.ledger.already_sent` scanning every entry for the
workspace (see `load_ledger_for_workspace`).

The API keeps using the CSV send loop as its working file during a `--send`
batch (that loop writes the ledger after every attempt, SPEC.md §10.2 step
5); `sync_entries` mirrors the finished ledger into Postgres afterwards.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from iff_scheduler.db.client import get_client
from iff_scheduler.domain.enums import SendStatus
from iff_scheduler.domain.models import SendLedgerEntry
from iff_scheduler.notify.ledger import make_ledger_id

_TABLE = "send_ledger"
_RUNS = "runs"
_ON_CONFLICT = "run_id,applicant_id,template"


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _run_ids_for_workspace(workspace_id: str) -> list[str]:
    resp = get_client().table(_RUNS).select("id").eq("workspace_id", workspace_id).execute()
    return [row["id"] for row in resp.data]


def _to_row(entry: SendLedgerEntry, run_pk: str) -> dict[str, Any]:
    return {
        "run_id": run_pk,
        "applicant_id": entry.applicant_id,
        "email": entry.email,
        "template": entry.template,
        "status": entry.status.value,
        "provider_message_id": entry.provider_message_id,
        "attempt_count": entry.attempt_count,
        "sent_at": entry.sent_at.isoformat() if entry.sent_at else None,
        "error": entry.error,
    }


def _to_entry(row: dict[str, Any]) -> SendLedgerEntry:
    joined = row.get("runs")
    run_label = joined.get("run_label", "") if isinstance(joined, dict) else ""
    return SendLedgerEntry(
        ledger_id=make_ledger_id(row["applicant_id"], row["template"]),
        applicant_id=row["applicant_id"],
        email=row["email"],
        template=row["template"],
        run_id=run_label,
        status=SendStatus(row["status"]),
        provider_message_id=row.get("provider_message_id") or None,
        attempt_count=int(row.get("attempt_count") or 0),
        sent_at=_parse_dt(row.get("sent_at")),
        error=row.get("error") or None,
    )


def load_ledger_for_workspace(workspace_id: str) -> list[SendLedgerEntry]:
    """Every ledger entry across the workspace's runs — the input to
    `already_sent` (FR-62)."""
    run_ids = _run_ids_for_workspace(workspace_id)
    if not run_ids:
        return []
    resp = get_client().table(_TABLE).select("*, runs(run_label)").in_("run_id", run_ids).execute()
    return [_to_entry(row) for row in resp.data]


def sync_entries(run_pk: str, run_label: str, entries: list[SendLedgerEntry]) -> None:
    """Upsert this run's ledger rows. Entries carrying a different `run_id`
    (older runs, already persisted under their own run) are left alone."""
    rows = [_to_row(entry, run_pk) for entry in entries if entry.run_id == run_label]
    if rows:
        get_client().table(_TABLE).upsert(rows, on_conflict=_ON_CONFLICT).execute()
