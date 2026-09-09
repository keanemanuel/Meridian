"""Shared FastAPI dependencies (beta, SPEC.md §14 "Beta").

The API is a thin wrapper over the alpha core: it calls the same functions
the CLI calls and never modifies `src/iff_scheduler/`. Everything here is
resolution and lookup — no scheduling logic lives in the API layer.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException

from api.credentials_bootstrap import credential_problem
from iff_scheduler import workspace as ws
from iff_scheduler.db import supabase_enabled
from iff_scheduler.settings import DEFAULT_CONFIG_DIR, Settings, load_settings
from iff_scheduler.workspace import WorkspaceMeta, find_workspace, load_workspaces

SERVICE_ACCOUNT_VAR = "GOOGLE_SERVICE_ACCOUNT_FILE"

# A Google service account key always carries these. Checking them catches
# the other common paste slip: the OAuth *client* JSON (which nests
# everything under "installed"/"web") pasted into the service-account var.
_SERVICE_ACCOUNT_KEYS = frozenset({"type", "client_email", "private_key", "token_uri"})


def service_account_file() -> str:
    """Path to a readable, well-formed Google service account key.

    Everything that talks to Google as the service account — the Sheets
    ingest and the Sheets export — goes through here, so a broken credential
    is reported once, in terms of the variable to fix, rather than surfacing
    as whatever `json.load` happened to say about a file the caller never
    mentioned.
    """
    problem = credential_problem(SERVICE_ACCOUNT_VAR)
    if problem is not None:
        raise HTTPException(status_code=409, detail=problem.message)

    value = os.environ.get(SERVICE_ACCOUNT_VAR)
    if not value:
        raise HTTPException(
            status_code=409,
            detail=f"{SERVICE_ACCOUNT_VAR} is not set in the environment.",
        )

    path = Path(value)
    if not path.is_file():
        raise HTTPException(
            status_code=409,
            detail=(
                f"{SERVICE_ACCOUNT_VAR} points at '{value}', which is not a readable "
                "file. Set it to the service account key's path, or paste the key "
                "JSON itself as the value."
            ),
        )

    try:
        key = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The service account key at '{value}' is not valid JSON: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno}). Re-paste the whole key "
                f"file into {SERVICE_ACCOUNT_VAR}."
            ),
        ) from exc

    missing = sorted(_SERVICE_ACCOUNT_KEYS - set(key)) if isinstance(key, dict) else ["*"]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The JSON in {SERVICE_ACCOUNT_VAR} is missing {', '.join(missing)}, so "
                "it is not a Google service account key. The OAuth client file used for "
                "Gmail is a different file and will not work here."
            ),
        )
    return str(path)


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


def run_dir_if_present(workspace_id: str, run_id: str) -> Path | None:
    """The run's directory when it is on this machine's disk, else None.

    In Supabase mode the run directory is a local artefact, not the record: a
    run solved before a redeploy (Railway's filesystem is ephemeral) or on a
    different instance has its rows in Postgres and no directory here.
    Requiring the directory there turned every such run into a 404 the moment
    it was opened, edited or re-solved.
    """
    resolve_workspace(workspace_id)
    run_dir = ws.runs_dir(workspace_id) / run_id
    return run_dir if run_dir.exists() else None


def ensure_run_exists(workspace_id: str, run_id: str) -> str:
    """404 unless the run exists in whichever store is live, and return its
    resolved label ('latest' resolved to the real one)."""
    if supabase_enabled():
        from iff_scheduler.db import run_repo

        row = run_repo.get_run(workspace_pk(workspace_id), run_id)
        if row is not None:
            return str(row["run_label"])

    run_dir = run_dir_if_present(workspace_id, run_id)
    if run_dir is None:
        raise HTTPException(
            status_code=404,
            detail=f"Run '{run_id}' not found for workspace '{workspace_id}'.",
        )
    return run_dir.resolve().name
