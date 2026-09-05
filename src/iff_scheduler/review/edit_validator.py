"""Validates a recruiter-edited assignments file before it can be locked
(FR-42, SPEC.md §11, E-12).

`scheduling.base.validate_problem` protects a `SolveProblem` once locks are
already loaded into it; this module is the earlier line of defence, checking
a raw edited schedule for the four illegal states SPEC.md §11 names —
double-booked applicant, panel busy, room over capacity, slot outside the
grid — before any of it becomes a `Lock`. Pure: no I/O, no adapters, so it
doesn't matter whether the edit came from a CSV, a Google Sheet, or an
interactive editor.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from iff_scheduler.domain.models import Assignment, Panel, Room, Slot


@dataclass(frozen=True)
class EditViolation:
    """One reason an edited schedule was rejected. `applicant_id` is blank for
    a violation that isn't about one specific applicant (e.g. a busy panel)."""

    applicant_id: str
    code: str
    message: str


def validate_edits(
    assignments: Sequence[Assignment],
    panels: Sequence[Panel],
    rooms: Sequence[Room],
    slots: Sequence[Slot],
) -> list[EditViolation]:
    """Return every violation found; empty means the edit is legal.

    Never raises on a bad edit — the caller decides what "rejected with a
    clear message" (FR-42) looks like for its context (CLI output, a report
    row, ...). Order is deterministic (FR-35): grid/reference checks first,
    then double-booking, in the order SPEC.md §11 lists them.
    """
    violations: list[EditViolation] = []
    panels_by_id = {panel.id: panel for panel in panels}
    rooms_by_id = {room.id: room for room in rooms}
    slot_ids = {slot.slot_id for slot in slots}

    for assignment in assignments:
        if assignment.slot_id not in slot_ids:
            violations.append(
                EditViolation(
                    assignment.applicant_id,
                    "SLOT_OUTSIDE_GRID",
                    f"Choice {assignment.choice_index} for {assignment.applicant_id} is placed "
                    f"at slot '{assignment.slot_id}', which is not on the event grid.",
                )
            )
            continue

        panel = panels_by_id.get(assignment.panel_id)
        if panel is None:
            violations.append(
                EditViolation(
                    assignment.applicant_id,
                    "UNKNOWN_PANEL",
                    f"Choice {assignment.choice_index} for {assignment.applicant_id} is "
                    f"assigned to panel '{assignment.panel_id}', which does not exist.",
                )
            )
            continue

        if assignment.slot_id not in set(panel.active_slot_ids):
            violations.append(
                EditViolation(
                    assignment.applicant_id,
                    "PANEL_INACTIVE",
                    f"Panel '{panel.id}' is not active at slot '{assignment.slot_id}' "
                    f"(C7) — cannot place {assignment.applicant_id} choice "
                    f"{assignment.choice_index} there.",
                )
            )

        if panel.division != assignment.division:
            violations.append(
                EditViolation(
                    assignment.applicant_id,
                    "DIVISION_MISMATCH",
                    f"Choice {assignment.choice_index} for {assignment.applicant_id} needs a "
                    f"{assignment.division.value} panel, but '{panel.id}' is "
                    f"{panel.division.value}.",
                )
            )

    # Double-booked applicant: the same person in two places in the same slot.
    by_applicant_slot: dict[tuple[str, str], list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        by_applicant_slot[(assignment.applicant_id, assignment.slot_id)].append(assignment)
    for (applicant_id, slot_id), rows in by_applicant_slot.items():
        if len(rows) > 1:
            choices = ", ".join(
                str(a.choice_index) for a in sorted(rows, key=lambda a: a.choice_index)
            )
            violations.append(
                EditViolation(
                    applicant_id,
                    "DOUBLE_BOOKED_APPLICANT",
                    f"{applicant_id} has {len(rows)} interviews at slot '{slot_id}' "
                    f"(choices {choices}) — an applicant cannot be in two places at once (C3).",
                )
            )

    # Panel busy: the same panel double-booked in the same slot by two choices.
    by_panel_slot: dict[tuple[str, str], list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        by_panel_slot[(assignment.panel_id, assignment.slot_id)].append(assignment)
    for (panel_id, slot_id), rows in by_panel_slot.items():
        if len(rows) > 1:
            who = ", ".join(sorted(f"{a.applicant_id}/choice {a.choice_index}" for a in rows))
            violations.append(
                EditViolation(
                    "",
                    "PANEL_BUSY",
                    f"Panel '{panel_id}' is booked for {len(rows)} interviews at slot "
                    f"'{slot_id}' ({who}) — a panel can only run one interview at a time (C2).",
                )
            )

    # Room over capacity: more panels active in a room+slot than it can hold.
    by_room_slot: dict[tuple[str, str], set[str]] = defaultdict(set)
    for assignment in assignments:
        by_room_slot[(assignment.room, assignment.slot_id)].add(assignment.panel_id)
    for (room_id, slot_id), panel_ids in by_room_slot.items():
        room = rooms_by_id.get(room_id)
        if room is None:
            violations.append(
                EditViolation(
                    "",
                    "UNKNOWN_ROOM",
                    f"Room '{room_id}' referenced at slot '{slot_id}' is not defined in "
                    "rooms config.",
                )
            )
            continue
        if len(panel_ids) > room.max_concurrent_panels:
            violations.append(
                EditViolation(
                    "",
                    "ROOM_OVER_CAPACITY",
                    f"Room '{room_id}' has {len(panel_ids)} panels active at slot "
                    f"'{slot_id}' ({', '.join(sorted(panel_ids))}), exceeding its "
                    f"max_concurrent_panels of {room.max_concurrent_panels} (C4).",
                )
            )

    return violations
