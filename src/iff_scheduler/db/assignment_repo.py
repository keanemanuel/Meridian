"""CRUD for the `assignments` table (beta replacement for assignments.csv).

`Assignment` in and `Assignment` out — the exact same domain object the
solver returns and the CLI writes to CSV. The one lossy detail: the schema
in 001_initial.sql has no `reason` column, so `Assignment.reason` is dropped
on write and comes back `None`. `is_locked` carries the fact that matters
(C6); the human-readable reason string was only ever a CSV convenience.
"""

from __future__ import annotations

from typing import Any

from iff_scheduler.db.client import get_client
from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.models import Assignment

_TABLE = "assignments"

# Columns that exist in 001_initial.sql AND map 1:1 to an Assignment field.
_COLUMNS = (
    "applicant_id",
    "full_name",
    "email",
    "choice_index",
    "sub_division",
    "division",
    "panel_id",
    "room",
    "slot_id",
    "date",
    "start_time",
    "end_time",
    "is_clash",
    "is_locked",
    "same_parent_pair",
)

_INSERT_CHUNK = 500


def _to_row(assignment: Assignment, run_pk: str) -> dict[str, Any]:
    data = assignment.model_dump(mode="json")
    row: dict[str, Any] = {col: data[col] for col in _COLUMNS}
    row["run_id"] = run_pk
    return row


def _to_assignment(row: dict[str, Any]) -> Assignment:
    return Assignment(
        applicant_id=row["applicant_id"],
        full_name=row["full_name"],
        email=row["email"],
        choice_index=1 if int(row["choice_index"]) == 1 else 2,
        sub_division=row["sub_division"],
        division=DivisionCode(row["division"]),
        panel_id=row["panel_id"],
        room=row["room"],
        slot_id=row["slot_id"],
        # PostgREST returns date/time columns as ISO strings; pydantic coerces
        # them to date/time on the way in, exactly as the CSV loader does.
        date=row["date"],
        start_time=row["start_time"],
        end_time=row["end_time"],
        is_clash=bool(row["is_clash"]),
        is_locked=bool(row["is_locked"]),
        same_parent_pair=bool(row["same_parent_pair"]),
        reason=None,
    )


def replace_assignments(run_pk: str, assignments: list[Assignment]) -> None:
    """A run's assignment set is immutable per solve, so replace wholesale:
    clear the run's rows, then bulk-insert. Chunked to stay under PostgREST's
    request-size limit at the 240-interview scale."""
    client = get_client()
    client.table(_TABLE).delete().eq("run_id", run_pk).execute()
    rows = [_to_row(a, run_pk) for a in assignments]
    for start in range(0, len(rows), _INSERT_CHUNK):
        client.table(_TABLE).insert(rows[start : start + _INSERT_CHUNK]).execute()


def list_assignments(run_pk: str) -> list[Assignment]:
    resp = (
        get_client()
        .table(_TABLE)
        .select("*")
        .eq("run_id", run_pk)
        .order("slot_id")
        .order("panel_id")
        .execute()
    )
    return [_to_assignment(row) for row in resp.data]


def update_assignment(
    run_pk: str, applicant_id: str, choice_index: int, fields: dict[str, Any]
) -> None:
    """Patch one interview in place — the manual-edit path (FR-41). The
    (run_id, applicant_id, choice_index) triple is unique within a run."""
    (
        get_client()
        .table(_TABLE)
        .update(fields)
        .eq("run_id", run_pk)
        .eq("applicant_id", applicant_id)
        .eq("choice_index", choice_index)
        .execute()
    )
