"""Workspace support (CLAUDE.md "Workspace support", SPEC.md §11).

A workspace is an isolated pipeline instance: its own applicant data, solve
history and ledgers. All data paths are namespaced under
`data/workspaces/<name>/` — there is no shared, un-namespaced data
directory, so two recruitment cycles never collide on disk.

This module does file I/O (workspace metadata, directory layout) and so —
like `settings.py` — sits outside `domain/`, `scheduling/` and `review/`,
which stay pure (CLAUDE.md, "Architecture rule").
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

DEFAULT_WORKSPACE = "default"


_REPO_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def workspaces_root() -> Path:
    """Where `data/workspaces` actually lives, resolved on every call.

    IFFSCHED_DATA_DIR wins when set — a mounted volume in a deploy, a tmp
    directory in a test. Otherwise it is the repo's own `data/`, located from
    this file rather than from `Path.cwd()`: the Railway start command runs
    uvicorn from `/app/src`, and a CWD-relative default would silently move
    every workspace's runs and ledgers into a second, empty tree.

    Read at call time, not bound into function defaults at import, so setting
    the environment variable actually redirects everything.
    """
    override = os.environ.get("IFFSCHED_DATA_DIR")
    return (Path(override) if override else _REPO_DATA_DIR) / "workspaces"


def workspaces_file() -> Path:
    return workspaces_root() / "workspaces.json"


class WorkspaceMeta(BaseModel):
    """One entry in `workspaces.json` (SPEC.md §11.2)."""

    model_config = ConfigDict(frozen=True)

    name: str
    group: str
    created_at: datetime


def load_workspaces(path: Path | None = None) -> list[WorkspaceMeta]:
    """Missing file means no workspace has been created yet — not an error."""
    path = path or workspaces_file()
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [WorkspaceMeta.model_validate(row) for row in data]


def save_workspaces(workspaces: list[WorkspaceMeta], path: Path | None = None) -> None:
    path = path or workspaces_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [w.model_dump(mode="json") for w in workspaces]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def find_workspace(name: str, workspaces: list[WorkspaceMeta]) -> WorkspaceMeta | None:
    return next((w for w in workspaces if w.name == name), None)


def create_workspace(
    name: str, group: str, path: Path | None = None, root: Path | None = None
) -> WorkspaceMeta:
    """Register a new workspace and lay down its directory skeleton
    (CLAUDE.md repo structure: `interim/`, `runs/`)."""
    workspaces = load_workspaces(path)
    if find_workspace(name, workspaces) is not None:
        raise ValueError(f"Workspace '{name}' already exists.")
    meta = WorkspaceMeta(name=name, group=group, created_at=datetime.now())
    save_workspaces([*workspaces, meta], path)
    interim_dir(name, root).mkdir(parents=True, exist_ok=True)
    runs_dir(name, root).mkdir(parents=True, exist_ok=True)
    return meta


def rename_workspace(
    old_name: str,
    new_name: str,
    path: Path | None = None,
    root: Path | None = None,
) -> WorkspaceMeta:
    """Rename a workspace and move its data directory with it.

    The name is the workspace's identity in alpha (SPEC.md §11.2), so the
    directory has to follow or every namespaced path would point at the old
    tree. Nothing is merged: if a directory already sits under the new name
    the rename is refused rather than guessing which tree is current
    (CLAUDE.md invariant 3).
    """
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Workspace name must not be blank.")

    workspaces = load_workspaces(path)
    existing = find_workspace(old_name, workspaces)
    if existing is None:
        raise ValueError(f"Workspace '{old_name}' not found.")
    if new_name == old_name:
        return existing
    if find_workspace(new_name, workspaces) is not None:
        raise ValueError(f"Workspace '{new_name}' already exists.")

    old_root, new_root = workspace_root(old_name, root), workspace_root(new_name, root)
    if new_root.exists():
        raise ValueError(f"A data directory already exists at {new_root}. Move or remove it first.")

    renamed = existing.model_copy(update={"name": new_name})
    save_workspaces([renamed if w.name == old_name else w for w in workspaces], path)
    if old_root.exists():
        old_root.rename(new_root)
    return renamed


# --------------------------------------------------------- namespaced paths


def workspace_root(name: str, root: Path | None = None) -> Path:
    return (root or workspaces_root()) / name


def interim_dir(name: str, root: Path | None = None) -> Path:
    return workspace_root(name, root) / "interim"


def raw_dir(name: str, root: Path | None = None) -> Path:
    return workspace_root(name, root) / "raw"


def locks_dir(name: str, root: Path | None = None) -> Path:
    return workspace_root(name, root) / "locks"


def ledger_dir(name: str, root: Path | None = None) -> Path:
    return workspace_root(name, root) / "ledger"


def runs_dir(name: str, root: Path | None = None) -> Path:
    return workspace_root(name, root) / "runs"


def output_dir(name: str, root: Path | None = None) -> Path:
    return workspace_root(name, root) / "output"


def applicants_clean_path(name: str, root: Path | None = None) -> Path:
    return interim_dir(name, root) / "applicants.clean.csv"


def validation_report_path(name: str, root: Path | None = None) -> Path:
    return interim_dir(name, root) / "validation_report.csv"


def locks_path(name: str, root: Path | None = None) -> Path:
    return locks_dir(name, root) / "pinned_assignments.csv"


def send_ledger_path(name: str, root: Path | None = None) -> Path:
    return ledger_dir(name, root) / "send_ledger.csv"


def scores_path(name: str, root: Path | None = None) -> Path:
    return raw_dir(name, root) / "scores.csv"
