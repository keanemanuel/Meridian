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
    extract_sheet_id,
    find_workspace,
    interim_dir,
    load_workspaces,
    rename_workspace,
    runs_dir,
    save_workspaces,
    set_workspace_sheet,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


class WorkspaceCreate(BaseModel):
    name: str
    group: str
    # Optional so the UI's "+ New" form can link a Sheet in the same step
    # (a full Sheets URL or a bare ID). Blank / whitespace means "no sheet".
    sheet_url: str | None = None


class SheetLink(BaseModel):
    sheet_url: str


class WorkspaceRename(BaseModel):
    name: str


# Groups whose workspaces hold live recruitment submissions. Renaming one is
# allowed (the UI warns first); deleting one is not, because the applicant
# data and every solve under it go with it and there is no undo.
PROTECTED_GROUPS = frozenset({"IFF Submissions"})

CANNOT_DELETE_LIVE = "Live submission workspaces cannot be deleted."


def _scaffold_dirs(name: str) -> None:
    interim_dir(name).mkdir(parents=True, exist_ok=True)
    runs_dir(name).mkdir(parents=True, exist_ok=True)


def _relocate_workspace_dir(old_name: str, new_name: str) -> None:
    """Move a workspace's on-disk tree to sit under its new name.

    In Supabase mode `workspace_repo.rename_workspace` renames the row in
    place, but the namespaced data directory (ingested applicants, every
    immutable `runs/<timestamp>/`) is keyed by name and would otherwise be
    orphaned under the old one — so a `check` or re-solve under the new name
    would 404. The file store already moves it (`rename_workspace`); this
    brings the DB path in line.
    """
    if old_name != new_name:
        old_root = ws.workspace_root(old_name)
        new_root = ws.workspace_root(new_name)
        if old_root.exists() and not new_root.exists():
            new_root.parent.mkdir(parents=True, exist_ok=True)
            old_root.rename(new_root)
    _scaffold_dirs(new_name)


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
    """Create a workspace, optionally linking a Google Sheet in the same
    request so the UI never has to fall back to `iffsched workspace
    set-sheet`."""
    raw_url = (body.sheet_url or "").strip()
    sheet_id = extract_sheet_id(raw_url) if raw_url else None
    try:
        if supabase_enabled():
            from iff_scheduler.db import workspace_repo

            meta = workspace_repo.create_workspace(body.name, body.group, sheet_id=sheet_id)
            _scaffold_dirs(body.name)
            return meta
        meta = create_workspace(body.name, body.group)
        if sheet_id is not None:
            meta = set_workspace_sheet(body.name, sheet_id)
        return meta
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/{workspace_id}", response_model=WorkspaceMeta)
def get_workspace(workspace_id: str) -> WorkspaceMeta:
    return resolve_workspace(workspace_id)


@router.patch("/{workspace_id}/sheet", response_model=WorkspaceMeta)
def patch_workspace_sheet(workspace_id: str, body: SheetLink) -> WorkspaceMeta:
    """Attach or replace the workspace's linked Google Sheet from a full
    Sheets URL (or a bare ID). The HTTP twin of `iffsched workspace
    set-sheet`."""
    sheet_id = extract_sheet_id(body.sheet_url.strip())
    if not sheet_id:
        raise HTTPException(status_code=422, detail="sheet_url must not be empty.")
    try:
        if supabase_enabled():
            from iff_scheduler.db import workspace_repo

            return workspace_repo.set_sheet(workspace_id, sheet_id)
        return set_workspace_sheet(workspace_id, sheet_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.patch("/{workspace_id}", response_model=WorkspaceMeta)
def patch_workspace_name(workspace_id: str, body: WorkspaceRename) -> WorkspaceMeta:
    """Rename a workspace. The name is the workspace's id in alpha, so the
    file store also moves the data directory (`rename_workspace`); in
    Supabase mode `runs` link by UUID and follow the row untouched."""
    try:
        if supabase_enabled():
            from iff_scheduler.db import workspace_repo

            meta = workspace_repo.rename_workspace(workspace_id, body.name)
            _relocate_workspace_dir(workspace_id, meta.name)
            return meta
        return rename_workspace(workspace_id, body.name)
    except ValueError as exc:
        message = str(exc)
        code = status.HTTP_404_NOT_FOUND if "not found" in message else status.HTTP_409_CONFLICT
        raise HTTPException(status_code=code, detail=message) from exc


@router.delete("/{workspace_id}", status_code=status.HTTP_200_OK)
def delete_workspace(workspace_id: str) -> dict[str, str]:
    """Delete a workspace and its data directory.

    Refused for a workspace in a protected group: that data is a live intake
    round and removing it is not recoverable. The UI hides the button too,
    but the rule is enforced here so it cannot be clicked past."""
    if resolve_workspace(workspace_id).group in PROTECTED_GROUPS:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CANNOT_DELETE_LIVE)

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
