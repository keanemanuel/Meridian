"""Tests for M9 workspace support (CLAUDE.md "Workspace support", SPEC.md §11).

A workspace isolates one recruitment cycle's data. These tests cover:

1. Metadata round-trips through workspaces.json (create, list).
2. Every namespaced path lives under data/workspaces/<name>/ — there is no
   shared, un-namespaced data directory (CLAUDE.md invariant).
3. Duplicate creation and unknown-workspace updates fail loudly rather than
   silently overwriting or guessing (CLAUDE.md invariant 3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from iff_scheduler.workspace import (
    applicants_clean_path,
    create_workspace,
    find_workspace,
    interim_dir,
    ledger_dir,
    load_workspaces,
    locks_dir,
    locks_path,
    output_dir,
    raw_dir,
    rename_workspace,
    runs_dir,
    save_workspaces,
    scores_path,
    send_ledger_path,
    validation_report_path,
    workspace_root,
)


@pytest.fixture
def workspaces_file(tmp_path: Path) -> Path:
    return tmp_path / "workspaces.json"


@pytest.fixture
def workspaces_root(tmp_path: Path) -> Path:
    return tmp_path / "workspaces"


def test_load_workspaces_missing_file_is_empty_not_an_error(workspaces_file: Path) -> None:
    assert load_workspaces(workspaces_file) == []


def test_create_workspace_round_trips_through_json(
    workspace_name: str, workspaces_file: Path, workspaces_root: Path
) -> None:
    meta = create_workspace(workspace_name, "IFF Submissions", workspaces_file, workspaces_root)

    assert meta.name == workspace_name
    assert meta.group == "IFF Submissions"

    reloaded = load_workspaces(workspaces_file)
    assert reloaded == [meta]


def test_create_workspace_lays_down_interim_and_runs_dirs(
    workspace_name: str, workspaces_file: Path, workspaces_root: Path
) -> None:
    create_workspace(workspace_name, "IFF Submissions", workspaces_file, workspaces_root)

    assert interim_dir(workspace_name, workspaces_root).is_dir()
    assert runs_dir(workspace_name, workspaces_root).is_dir()


def test_create_workspace_duplicate_name_rejected(
    workspace_name: str, workspaces_file: Path, workspaces_root: Path
) -> None:
    create_workspace(workspace_name, "Group A", workspaces_file, workspaces_root)

    with pytest.raises(ValueError, match="already exists"):
        create_workspace(workspace_name, "Group B", workspaces_file, workspaces_root)


def test_find_workspace_returns_none_when_absent(workspace_name: str) -> None:
    assert find_workspace(workspace_name, []) is None


def test_save_workspaces_overwrites_full_list(workspaces_file: Path, workspaces_root: Path) -> None:
    a = create_workspace("a", "Group A", workspaces_file, workspaces_root)
    b = create_workspace("b", "Group B", workspaces_file, workspaces_root)

    save_workspaces([a], workspaces_file)

    assert [w.name for w in load_workspaces(workspaces_file)] == ["a"]
    assert b.name == "b"  # b's in-memory object is untouched by the overwrite


# ---------------------------------------------------- namespaced data paths


def test_every_data_path_is_namespaced_under_the_workspace(
    workspace_name: str, workspaces_root: Path
) -> None:
    root = workspace_root(workspace_name, workspaces_root)

    paths = [
        interim_dir(workspace_name, workspaces_root),
        raw_dir(workspace_name, workspaces_root),
        locks_dir(workspace_name, workspaces_root),
        ledger_dir(workspace_name, workspaces_root),
        runs_dir(workspace_name, workspaces_root),
        output_dir(workspace_name, workspaces_root),
        applicants_clean_path(workspace_name, workspaces_root),
        validation_report_path(workspace_name, workspaces_root),
        locks_path(workspace_name, workspaces_root),
        send_ledger_path(workspace_name, workspaces_root),
        scores_path(workspace_name, workspaces_root),
    ]

    for path in paths:
        assert root in path.parents or path == root, f"{path} is not namespaced under {root}"


def test_two_workspaces_never_share_a_path(workspaces_root: Path) -> None:
    assert applicants_clean_path("a", workspaces_root) != applicants_clean_path(
        "b", workspaces_root
    )
    assert runs_dir("a", workspaces_root) != runs_dir("b", workspaces_root)


# ---- rename (M9: the name is the workspace's id, so its data moves too) ----


def test_rename_moves_the_metadata_and_the_data_directory(
    workspaces_file: Path, tmp_path: Path
) -> None:
    root = tmp_path / "workspaces"
    create_workspace("Old Name", "Test Environment", workspaces_file, root)
    (interim_dir("Old Name", root) / "applicants.clean.csv").write_text("x", encoding="utf-8")

    renamed = rename_workspace("Old Name", "New Name", workspaces_file, root)

    assert renamed.name == "New Name"
    assert renamed.group == "Test Environment"
    assert [w.name for w in load_workspaces(workspaces_file)] == ["New Name"]
    assert not workspace_root("Old Name", root).exists()
    assert (interim_dir("New Name", root) / "applicants.clean.csv").read_text() == "x"


def test_rename_to_the_same_name_is_a_no_op(workspaces_file: Path, tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    create_workspace("Same", "Test Environment", workspaces_file, root)
    assert rename_workspace("Same", "Same", workspaces_file, root).name == "Same"
    assert workspace_root("Same", root).exists()


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("Missing", "Whatever", "not found"),
        ("A", "B", "already exists"),
        ("A", "   ", "must not be blank"),
    ],
)
def test_rename_fails_loudly_rather_than_overwriting(
    workspaces_file: Path, tmp_path: Path, old: str, new: str, expected: str
) -> None:
    root = tmp_path / "workspaces"
    create_workspace("A", "Test Environment", workspaces_file, root)
    create_workspace("B", "Test Environment", workspaces_file, root)

    with pytest.raises(ValueError, match=expected):
        rename_workspace(old, new, workspaces_file, root)
