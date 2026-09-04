"""Room view: one timetable per room per day (FR-50).

Rows are slots, columns are panels, cells are applicant + division. Pure —
takes the plain domain objects a solve already produced and returns plain
view objects; the writers in `xlsx_writer` / `html_writer` decide how those
become files (CLAUDE.md, "Architecture rule").
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as Date
from datetime import time as Time

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.models import Assignment, ChoiceIndex, Panel, Room, Slot


@dataclass(frozen=True)
class RoomViewCell:
    """One occupied (panel, slot) cell."""

    applicant_id: str
    full_name: str
    sub_division: str
    division: DivisionCode
    choice_index: ChoiceIndex
    is_clash: bool
    is_locked: bool


@dataclass(frozen=True)
class RoomViewRow:
    """One slot row across every panel column in the room."""

    slot_id: str
    start_time: Time
    end_time: Time
    cells: dict[str, RoomViewCell | None]  # panel_id -> cell, None if empty


@dataclass(frozen=True)
class RoomView:
    """One room's timetable for one day."""

    room_id: str
    date: Date
    day_label: str
    panel_ids: list[str]  # column order, deterministic
    panel_divisions: dict[str, DivisionCode]
    rows: list[RoomViewRow]


def build_room_views(
    assignments: Sequence[Assignment],
    panels: Sequence[Panel],
    rooms: Sequence[Room],
    slots: Sequence[Slot],
) -> list[RoomView]:
    """Build one `RoomView` per (room, day) that has at least one panel."""
    panels_by_room: dict[str, list[Panel]] = defaultdict(list)
    for panel in panels:
        panels_by_room[panel.room].append(panel)

    by_panel_slot: dict[tuple[str, str], Assignment] = {
        (a.panel_id, a.slot_id): a for a in assignments
    }

    dates = sorted({slot.date for slot in slots})
    views: list[RoomView] = []

    for room in rooms:
        room_panels = sorted(panels_by_room.get(room.id, []), key=lambda p: p.id)
        if not room_panels:
            continue
        panel_ids = [p.id for p in room_panels]
        panel_divisions = {p.id: p.division for p in room_panels}

        for day in dates:
            day_slots = sorted((s for s in slots if s.date == day), key=lambda s: s.slot_index)
            if not day_slots:
                continue

            rows: list[RoomViewRow] = []
            for slot in day_slots:
                cells: dict[str, RoomViewCell | None] = {}
                for panel_id in panel_ids:
                    a = by_panel_slot.get((panel_id, slot.slot_id))
                    cells[panel_id] = (
                        RoomViewCell(
                            applicant_id=a.applicant_id,
                            full_name=a.full_name,
                            sub_division=a.sub_division,
                            division=a.division,
                            choice_index=a.choice_index,
                            is_clash=a.is_clash,
                            is_locked=a.is_locked,
                        )
                        if a is not None
                        else None
                    )
                rows.append(
                    RoomViewRow(
                        slot_id=slot.slot_id,
                        start_time=slot.start_time,
                        end_time=slot.end_time,
                        cells=cells,
                    )
                )

            views.append(
                RoomView(
                    room_id=room.id,
                    date=day,
                    day_label=day_slots[0].day_label,
                    panel_ids=panel_ids,
                    panel_divisions=panel_divisions,
                    rows=rows,
                )
            )

    return views
