"""Locks: manual decisions the solver must treat as fixed (C6, FR-41, SPEC.md §11).

`scheduling.base.Lock` is the type the solver consumes. This module is the
pure logic around it — turning an `Assignment` into a `Lock`, parsing rows
read from `data/locks/pinned_assignments.csv`, and merging cumulatively so a
re-lock overwrites the stale pin for that choice rather than duplicating it.
Reading and writing the CSV itself is the CLI's job (`iffsched lock`), the
same split `ingest/normalize.py` uses for applicant rows: pure parsing here,
pandas at the edge.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from iff_scheduler.domain.models import Assignment
from iff_scheduler.scheduling.base import Lock

LOCK_COLUMNS = ["applicant_id", "choice_index", "panel_id", "slot_id"]


def lock_from_assignment(assignment: Assignment) -> Lock:
    """One row of a (validated) assignments file, pinned as-is (SPEC.md §11)."""
    return Lock(
        applicant_id=assignment.applicant_id,
        choice_index=assignment.choice_index,
        panel_id=assignment.panel_id,
        slot_id=assignment.slot_id,
    )


def parse_lock_rows(rows: Iterable[Mapping[str, str]]) -> list[Lock]:
    """Parse already-read CSV rows (dicts of strings) into `Lock`s.

    Accepts any row source carrying the four columns that identify a pin —
    a dedicated locks file or a solved run's `assignments.csv` — so `iffsched
    lock --from runs/latest/assignments.csv` needs no separate format.
    """
    locks: list[Lock] = []
    for row in rows:
        missing = [c for c in LOCK_COLUMNS if c not in row]
        if missing:
            raise ValueError(f"Lock row is missing column(s) {missing}: {dict(row)}")
        raw_choice = row["choice_index"]
        try:
            choice_index = int(raw_choice)
        except ValueError as exc:
            raise ValueError(
                f"choice_index must be an integer, got {raw_choice!r} "
                f"for applicant {row['applicant_id']!r}"
            ) from exc
        if choice_index not in (1, 2):
            raise ValueError(
                f"choice_index must be 1 or 2, got {choice_index!r} "
                f"for applicant {row['applicant_id']!r}"
            )
        locks.append(
            Lock(
                applicant_id=row["applicant_id"],
                choice_index=1 if choice_index == 1 else 2,
                panel_id=row["panel_id"],
                slot_id=row["slot_id"],
            )
        )
    return locks


def lock_to_row(lock: Lock) -> dict[str, str]:
    return {
        "applicant_id": lock.applicant_id,
        "choice_index": str(lock.choice_index),
        "panel_id": lock.panel_id,
        "slot_id": lock.slot_id,
    }


def merge_locks(existing: Sequence[Lock], incoming: Sequence[Lock]) -> list[Lock]:
    """Cumulative merge keyed by (applicant_id, choice_index).

    "Locks are cumulative and explicit" (SPEC.md §11): re-locking a choice
    that was already pinned replaces its old pin rather than creating a
    duplicate row, so a recruiter can move a locked interview and re-lock it
    without first clearing everything. Sorted for determinism (FR-35).
    """
    merged: dict[tuple[str, int], Lock] = {
        (lock.applicant_id, lock.choice_index): lock for lock in existing
    }
    for lock in incoming:
        merged[(lock.applicant_id, lock.choice_index)] = lock
    return [merged[key] for key in sorted(merged)]
