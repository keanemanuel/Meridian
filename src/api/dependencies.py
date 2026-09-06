"""Shared FastAPI dependencies (beta, SPEC.md §14 "Beta").

The API is a thin wrapper over the alpha core: it calls the same functions
the CLI calls and never modifies `src/iff_scheduler/`. Everything here is
resolution and lookup — no scheduling logic lives in the API layer.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

from iff_scheduler import workspace as ws
from iff_scheduler.db import supabase_enabled
from iff_scheduler.settings import DEFAULT_CONFIG_DIR, Settings, load_settings
from iff_scheduler.workspace import WorkspaceMeta, find_workspace, load_workspaces


def config_dir() -> Path:
    """Config directory, overridable with IFFSCHED_CONFIG_DIR for tests/deploys."""
    override = os.environ.get("IFFSCHED_CONFIG_DIR")
    return Path(override) if override else DEFAULT_CONFIG_DIR


@lru_cache(maxsize=8)
def _load_settings_cached(config_path: str) -> Settings:
    return load_settings(Path(config_path))


def get_settings() -> Settings:
    """Loaded-and-validated config for the current run (SPEC.md §8).

    Cached by directory so a malformed YAML still fails loudly the first time.
    """
    return _load_settings_cached(str(config_dir()))


def resolve_workspace(workspace_id: str) -> WorkspaceMeta:
    """Look a workspace up by name (its id in alpha; SPEC.md §11.2).

    Reads from Postgres when the Supabase backend is configured, otherwise
    from `workspaces.json`. 404s rather than guessing a default — CLAUDE.md
    invariant 3.
    """
    if supabase_enabled():
        from iff_scheduler.db import workspace_repo

        meta = workspace_repo.get_workspace(workspace_id)
    else:
        meta = find_workspace(workspace_id, load_workspaces())
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found.")
    return meta


def workspace_pk(workspace_id: str) -> str:
    """The workspace's Postgres UUID. Supabase-mode only — raises otherwise."""
    from iff_scheduler.db import workspace_repo

    pk = workspace_repo.get_workspace_id(workspace_id)
    if pk is None:
        raise HTTPException(status_code=404, detail=f"Workspace '{workspace_id}' not found.")
    return pk


def resolve_run_pk(workspace_id: str, run_id: str) -> str:
    """Resolve a run's Postgres UUID from its label (accepts 'latest').

    Supabase-mode only; callers guard with `supabase_enabled()` and fall
    back to `resolve_run_dir` for the file store.
    """
    from iff_scheduler.db import run_repo

    row = run_repo.get_run(workspace_pk(workspace_id), run_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Run '{run_id}' not found for workspace '{workspace_id}'.",
        )
    return str(row["id"])


def resolve_run_dir(workspace_id: str, run_id: str) -> Path:
    """Resolve `runs/<run_id>` for a workspace, accepting 'latest'."""
    resolve_workspace(workspace_id)
    run_dir = ws.runs_dir(workspace_id) / run_id
    if not run_dir.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Run '{run_id}' not found for workspace '{workspace_id}'.",
        )
    return run_dir
