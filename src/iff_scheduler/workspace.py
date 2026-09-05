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
import re
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

DEFAULT_WORKSPACE = "default"

WORKSPACES_ROOT = Path("data/workspaces")
WORKSPACES_FILE = WORKSPACES_ROOT / "workspaces.json"

_SHEET_URL_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")


class WorkspaceMeta(BaseModel):
    """One entry in `workspaces.json` (SPEC.md §11.2)."""

    model_config = ConfigDict(frozen=True)

    name: str
    group: str
    sheet_id: str | None = None
    created_at: datetime


def extract_sheet_id(url_or_id: str) -> str:
    """Pull the Sheet ID out of a full Google Sheets URL, or pass through a
    bare ID unchanged."""
    match = _SHEET_URL_ID_RE.search(url_or_id)
    return match.group(1) if match else url_or_id


def load_workspaces(path: Path = WORKSPACES_FILE) -> list[WorkspaceMeta]:
    """Missing file means no workspace has been created yet — not an error."""
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [WorkspaceMeta.model_validate(row) for row in data]


def save_workspaces(workspaces: list[WorkspaceMeta], path: Path = WORKSPACES_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [w.model_dump(mode="json") for w in workspaces]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def find_workspace(name: str, workspaces: list[WorkspaceMeta]) -> WorkspaceMeta | None:
    return next((w for w in workspaces if w.name == name), None)


def create_workspace(
    name: str, group: str, path: Path = WORKSPACES_FILE, root: Path = WORKSPACES_ROOT
) -> WorkspaceMeta:
    """Register a new workspace and lay down its directory skeleton
    (CLAUDE.md repo structure: `interim/`, `runs/`)."""
    workspaces = load_workspaces(path)
    if find_workspace(name, workspaces) is not None:
        raise ValueError(f"Workspace '{name}' already exists.")
    meta = WorkspaceMeta(name=name, group=group, sheet_id=None, created_at=datetime.now())
    save_workspaces([*workspaces, meta], path)
    interim_dir(name, root).mkdir(parents=True, exist_ok=True)
    runs_dir(name, root).mkdir(parents=True, exist_ok=True)
    return meta


def set_workspace_sheet(name: str, url_or_id: str, path: Path = WORKSPACES_FILE) -> WorkspaceMeta:
    workspaces = load_workspaces(path)
    existing = find_workspace(name, workspaces)
    if existing is None:
        raise ValueError(f"Workspace '{name}' not found. Run `iffsched workspace create` first.")
    updated = existing.model_copy(update={"sheet_id": extract_sheet_id(url_or_id)})
    save_workspaces([updated if w.name == name else w for w in workspaces], path)
    return updated


# --------------------------------------------------------- namespaced paths


def workspace_root(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return root / name


def interim_dir(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return workspace_root(name, root) / "interim"


def raw_dir(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return workspace_root(name, root) / "raw"


def locks_dir(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return workspace_root(name, root) / "locks"


def ledger_dir(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return workspace_root(name, root) / "ledger"


def runs_dir(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return workspace_root(name, root) / "runs"


def output_dir(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return workspace_root(name, root) / "output"


def last_ingested_row_path(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return workspace_root(name, root) / "last_ingested_row.txt"


def applicants_clean_path(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return interim_dir(name, root) / "applicants.clean.csv"


def validation_report_path(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return interim_dir(name, root) / "validation_report.csv"


def locks_path(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return locks_dir(name, root) / "pinned_assignments.csv"


def send_ledger_path(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return ledger_dir(name, root) / "send_ledger.csv"


def scores_path(name: str, root: Path = WORKSPACES_ROOT) -> Path:
    return raw_dir(name, root) / "scores.csv"
