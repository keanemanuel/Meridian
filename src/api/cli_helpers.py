"""Re-exports of the CLI's private CSV<->domain helpers.

The API deliberately reuses the exact serialisation the CLI uses so a run
directory written by `iffsched solve` and one written by `POST /solve` are
byte-for-byte the same shape. `iff_scheduler/` is never modified — this
module just gives those helpers public names for the API layer.
"""

from __future__ import annotations

from iff_scheduler.cli import (
    ASSIGNMENT_COLUMNS,
    CONFLICT_COLUMNS,
    _assignments_frame,
    _conflicts_frame,
    _load_assignments,
    _load_clean_applicants,
    _load_ledger,
    _load_locks,
    _send_batch,
    _write_ledger,
    _write_locks,
)

__all__ = [
    "ASSIGNMENT_COLUMNS",
    "CONFLICT_COLUMNS",
    "assignments_frame",
    "conflicts_frame",
    "load_assignments",
    "load_clean_applicants",
    "load_ledger",
    "load_locks",
    "send_batch",
    "write_ledger",
    "write_locks",
]

assignments_frame = _assignments_frame
conflicts_frame = _conflicts_frame
load_assignments = _load_assignments
load_clean_applicants = _load_clean_applicants
load_ledger = _load_ledger
load_locks = _load_locks
send_batch = _send_batch
write_ledger = _write_ledger
write_locks = _write_locks
