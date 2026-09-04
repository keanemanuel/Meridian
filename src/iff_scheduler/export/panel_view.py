"""Panel view: one sheet per panel, their running order for the day (FR-52)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date as Date
from datetime import time as Time

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.models import Assignment, ChoiceIndex, Panel, Slot


@dataclass(frozen=True)
class PanelViewRow:
    """One booked interview on a panel's running order."""

    slot_id: str
    date: Date
    start_time: Time
    end_time: Time
    applicant_id: str
    full_name: str
    sub_division: str
    division: DivisionCode
    choice_index: ChoiceIndex
    is_clash: bool
    is_locked: bool


@dataclass(frozen=True)
class PanelView:
    """One panel's running order, in slot order. Idle slots are omitted —
    this is a running order, not a full-grid timetable (that is `RoomView`'s
    job)."""

    panel_id: str
    division: DivisionCode
    room: str
    rows: list[PanelViewRow]


def build_panel_views(
    assignments: Sequence[Assignment],
    panels: Sequence[Panel],
    slots: Sequence[Slot],
) -> list[PanelView]:
    """One `PanelView` per configured panel, in panel-id order (deterministic)."""
    slot_index = {slot.slot_id: slot.slot_index for slot in slots}
    by_panel: dict[str, list[Assignment]] = defaultdict(list)
    for a in assignments:
        by_panel[a.panel_id].append(a)

    views: list[PanelView] = []
    for panel in sorted(panels, key=lambda p: p.id):
        booked = sorted(by_panel.get(panel.id, []), key=lambda a: slot_index[a.slot_id])
        rows = [
            PanelViewRow(
                slot_id=a.slot_id,
                date=a.date,
                start_time=a.start_time,
                end_time=a.end_time,
                applicant_id=a.applicant_id,
                full_name=a.full_name,
                sub_division=a.sub_division,
                division=a.division,
                choice_index=a.choice_index,
                is_clash=a.is_clash,
                is_locked=a.is_locked,
            )
            for a in booked
        ]
        views.append(
            PanelView(panel_id=panel.id, division=panel.division, room=panel.room, rows=rows)
        )
    return views
