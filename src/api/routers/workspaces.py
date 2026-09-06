"""Workspace CRUD (SPEC.md §11).

Alpha keys workspaces by name; that name is the `{id}` in these routes.
Metadata still lives in `data/workspaces/workspaces.json` (beta moves it to
Postgres — SPEC.md §11.4).
"""

from __future__ import annotations

import shutil

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from api.dependencies import resolve_workspace
from iff_scheduler import workspace as ws
from iff_scheduler.workspace import (
    WorkspaceMeta,
    create_workspace,
    find_workspace,
    load_workspaces,
    save_workspaces,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str
    group: str


@router.get("", response_model=list[WorkspaceMeta])
def list_workspaces() -> list[WorkspaceMeta]:
    return sorted(load_workspaces(), key=lambda w: (w.group, w.name))


@router.post("", response_model=WorkspaceMeta, status_code=status.HTTP_201_CREATED)
def post_workspace(body: WorkspaceCreate) -> WorkspaceMeta:
    try:
        return create_workspace(body.name, body.group)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{workspace_id}", response_model=WorkspaceMeta)
def get_workspace(workspace_id: str) -> WorkspaceMeta:
    return resolve_workspace(workspace_id)


@router.delete("/{workspace_id}", status_code=status.HTTP_200_OK)
def delete_workspace(workspace_id: str) -> dict[str, str]:
    workspaces = load_workspaces()
    if find_workspace(workspace_id, workspaces) is None:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found.")
    save_workspaces([w for w in workspaces if w.name != workspace_id])
    root = ws.workspace_root(workspace_id)
    if root.exists():
        shutil.rmtree(root)
    return {"deleted": workspace_id}
