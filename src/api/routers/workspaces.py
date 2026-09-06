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


@router.get("", response_model=list[WorkspaceMeta])
def list_workspaces() -> list[WorkspaceMeta]:
    if supabase_enabled():
        from iff_scheduler.db import workspace_repo

        return workspace_repo.list_workspaces()
    return sorted(load_workspaces(), key=lambda w: (w.group, w.name))


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
