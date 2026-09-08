"""Workspace CRUD (SPEC.md §11).

Alpha keys workspaces by name; that name is the `{id}` in these routes.
Metadata lives in `data/workspaces/workspaces.json` for the file store and
in the `workspaces` table when the Supabase backend is configured (SPEC.md
§11.4). Either way the workspace's local directory skeleton (`interim/`,
`runs/`) is still laid down — ingest writes files and every solve writes an
immutable run directory regardless of backend.
"""

from __future__ import annotations

import shutil
import sys
import traceback

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from api.dependencies import resolve_workspace
from iff_scheduler import workspace as ws
from iff_scheduler.db import supabase_enabled
from iff_scheduler.workspace import (
    WorkspaceMeta,
    create_workspace,
    find_workspace,
    interim_dir,
    load_workspaces,
    runs_dir,
    save_workspaces,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str
    group: str


def _scaffold_dirs(name: str) -> None:
    interim_dir(name).mkdir(parents=True, exist_ok=True)
    runs_dir(name).mkdir(parents=True, exist_ok=True)


def _list_from_file() -> list[WorkspaceMeta]:
    return sorted(load_workspaces(), key=lambda w: (w.group, w.name))


@router.get("", response_model=list[WorkspaceMeta])
def list_workspaces() -> list[WorkspaceMeta]:
    """List every workspace.

    Deploy resilience (docs/DEPLOY.md): a Supabase outage — or a
    misconfigured `SUPABASE_*` on the host — must not take the sidebar
    down. When the DB backend is enabled but the read raises, the full
    traceback goes to stderr (Railway captures stderr) and we fall back to
    the file store. Only a failure of *both* stores is a real error, and it
    is returned as a clean 503 rather than a bare 500.
    """
    if supabase_enabled():
        try:
            from iff_scheduler.db import workspace_repo

            return workspace_repo.list_workspaces()
        except Exception:
            print(
                "GET /api/workspaces: Supabase backend is enabled but the read "
                "failed — falling back to the file store.",
                file=sys.stderr,
            )
            traceback.print_exc()

    try:
        return _list_from_file()
    except Exception as exc:
        print(f"GET /api/workspaces: file-store read also failed: {exc!r}", file=sys.stderr)
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Workspace store unavailable: the Supabase read failed and the "
                "file-store fallback also failed. Check the service logs."
            ),
        ) from exc


@router.post("", response_model=WorkspaceMeta, status_code=status.HTTP_201_CREATED)
def post_workspace(body: WorkspaceCreate) -> WorkspaceMeta:
    try:
        if supabase_enabled():
            from iff_scheduler.db import workspace_repo

            meta = workspace_repo.create_workspace(body.name, body.group)
            _scaffold_dirs(body.name)
            return meta
        return create_workspace(body.name, body.group)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{workspace_id}", response_model=WorkspaceMeta)
def get_workspace(workspace_id: str) -> WorkspaceMeta:
    return resolve_workspace(workspace_id)


@router.delete("/{workspace_id}", status_code=status.HTTP_200_OK)
def delete_workspace(workspace_id: str) -> dict[str, str]:
    if supabase_enabled():
        from iff_scheduler.db import workspace_repo

        if not workspace_repo.delete_workspace(workspace_id):
            raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found.")
    else:
        workspaces = load_workspaces()
        if find_workspace(workspace_id, workspaces) is None:
            raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found.")
        save_workspaces([w for w in workspaces if w.name != workspace_id])

    root = ws.workspace_root(workspace_id)
    if root.exists():
        shutil.rmtree(root)
    return {"deleted": workspace_id}
