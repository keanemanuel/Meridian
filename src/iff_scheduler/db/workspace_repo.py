"""CRUD for the `workspaces` table (beta replacement for workspaces.json).

Returns the same `WorkspaceMeta` the alpha file store returns, so callers
(the API's workspace router, `dependencies.resolve_workspace`) don't care
which backend is live. The DB column is `group_name`; the domain field is
`group` — mapped here and nowhere else.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from iff_scheduler.db.client import get_client
from iff_scheduler.workspace import WorkspaceMeta

_TABLE = "workspaces"


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _to_meta(row: dict[str, Any]) -> WorkspaceMeta:
    return WorkspaceMeta(
        name=row["name"],
        group=row["group_name"],
        sheet_id=row.get("sheet_id"),
        created_at=_parse_dt(row["created_at"]),
    )


def list_workspaces() -> list[WorkspaceMeta]:
    resp = get_client().table(_TABLE).select("*").order("group_name").order("name").execute()
    return [_to_meta(row) for row in resp.data]


def get_workspace(name: str) -> WorkspaceMeta | None:
    resp = get_client().table(_TABLE).select("*").eq("name", name).limit(1).execute()
    return _to_meta(resp.data[0]) if resp.data else None


def get_workspace_id(name: str) -> str | None:
    """The UUID primary key — needed to link `runs` rows."""
    resp = get_client().table(_TABLE).select("id").eq("name", name).limit(1).execute()
    return resp.data[0]["id"] if resp.data else None


def create_workspace(name: str, group: str, sheet_id: str | None = None) -> WorkspaceMeta:
    if get_workspace(name) is not None:
        raise ValueError(f"Workspace '{name}' already exists.")
    resp = (
        get_client()
        .table(_TABLE)
        .insert({"name": name, "group_name": group, "sheet_id": sheet_id})
        .execute()
    )
    return _to_meta(resp.data[0])


def set_sheet(name: str, sheet_id: str) -> WorkspaceMeta:
    resp = get_client().table(_TABLE).update({"sheet_id": sheet_id}).eq("name", name).execute()
    if not resp.data:
        raise ValueError(f"Workspace '{name}' not found.")
    return _to_meta(resp.data[0])


def delete_workspace(name: str) -> bool:
    """True if a row was removed. `runs`/`assignments`/`send_ledger` cascade."""
    resp = get_client().table(_TABLE).delete().eq("name", name).execute()
    return bool(resp.data)
