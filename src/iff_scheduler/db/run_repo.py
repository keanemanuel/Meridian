"""CRUD for the `runs` table (beta replacement for runs/<timestamp>/metrics.json).

`run_label` is the external identifier the API already uses everywhere — the
solve timestamp, e.g. `2026-09-06T14-30-00`. The UUID `id` ("run_pk" in this
package) is internal: it is what `assignments` and `send_ledger` link to.
"latest" resolves to the most recent row by `created_at`.
"""

from __future__ import annotations

from typing import Any

from iff_scheduler.db.client import get_client

_TABLE = "runs"


def create_run(
    workspace_id: str,
    run_label: str,
    *,
    status: str = "complete",
    metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resp = (
        get_client()
        .table(_TABLE)
        .insert(
            {
                "workspace_id": workspace_id,
                "run_label": run_label,
                "status": status,
                "metrics": metrics or {},
            }
        )
        .execute()
    )
    rows: list[dict[str, Any]] = resp.data
    return rows[0]


def latest_run(workspace_id: str) -> dict[str, Any] | None:
    resp = (
        get_client()
        .table(_TABLE)
        .select("*")
        .eq("workspace_id", workspace_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


def get_run(workspace_id: str, run_label: str) -> dict[str, Any] | None:
    if run_label == "latest":
        return latest_run(workspace_id)
    resp = (
        get_client()
        .table(_TABLE)
        .select("*")
        .eq("workspace_id", workspace_id)
        .eq("run_label", run_label)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


def list_runs(workspace_id: str) -> list[dict[str, Any]]:
    resp = (
        get_client()
        .table(_TABLE)
        .select("*")
        .eq("workspace_id", workspace_id)
        .order("created_at")
        .execute()
    )
    return list(resp.data)


def update_run(run_pk: str, **fields: Any) -> None:
    if fields:
        get_client().table(_TABLE).update(fields).eq("id", run_pk).execute()
