"""Shared pytest fixtures (CLAUDE.md, "Workspace support")."""

from __future__ import annotations

import pytest


@pytest.fixture
def workspace_name() -> str:
    """A workspace name to isolate a test's data under (SPEC.md §11)."""
    return "test-workspace"
