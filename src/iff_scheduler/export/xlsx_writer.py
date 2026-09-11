"""XLSX export (FR-53).

Three workbooks make up the web app's "Download XLSX" ZIP:

* `write_xlsx` — exactly one sheet per top-level DIVISION (whatever
  `config/divisions.yaml` declares, never a hardcoded count), laid out the
  way the committee's manual scheduling sheet reads: a day header ("Thursday,
  17 September"), then that day's rooms side by side as [Interviewer 1 |
  Interviewer 2 | Room N] column groups — one group per room the division
  actually used that day, never a fixed or empty placeholder room — then the
  next day's block below it. The Applicants tab, per-panel running orders and
  the conflicts report are separate outputs, not sheets in this file.
* `write_applicants_xlsx` — a single-sheet workbook mirroring the web app's
  Applicants tab exactly as displayed (FR-51).
* `write_rooms_xlsx` — a room-oriented overview: for each day, which
  panels/divisions are running in each room, for someone walking the venue.

Clashes are marked red (FR-54): a filled cell plus a coloured, bold font, so
the flag survives both screen viewing and black-and-white printing.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import date as Date
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from iff_scheduler.domain.models import Assignment, Room
from iff_scheduler.export.applicant_view import ApplicantChoiceView, ApplicantViewRow
from iff_scheduler.export.room_view import RoomView

RED_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
RED_FONT = Font(color="9C0006", bold=True)
HEADER_FILL = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
HEADER_FONT = Font(bold=True)
LOCKED_FONT = Font(italic=True)

# Cycled across the rooms in a day's block so each room's column group reads
# as its own visual unit (required final structure: "Room header cells keep
# distinct background coloring per room"). Kept light so RED_FILL clashes and
# black-and-white printing both still stand out against it.
ROOM_HEADER_FILLS = [
    PatternFill(start_color=color, end_color=color, fill_type="solid")
    for color in ("DDEBF7", "E2EFDA", "FCE4D6", "EAD1DC", "FFF2CC", "D9D2E9")
]
_THIN_SIDE = Side(style="thin", color="BFBFBF")
BLANK_COLUMN_BORDER = Border(left=_THIN_SIDE, right=_THIN_SIDE, top=_THIN_SIDE, bottom=_THIN_SIDE)

_INVALID_SHEET_CHARS = set("[]:*?/\\")
_MAX_SHEET_NAME = 31


def _sheet_name(raw: str) -> str:
    """Excel sheet names: <=31 chars, no `[]:*?/\\` (FR-50, FR-52 — one sheet
    per room/day or per panel, so names must survive real ids and labels)."""
    cleaned = "".join(c for c in raw if c not in _INVALID_SHEET_CHARS).strip()
    return (cleaned or "Sheet")[:_MAX_SHEET_NAME]


def _unique_sheet_name(wb: Workbook, raw: str) -> str:
    base = _sheet_name(raw)
    existing = {ws.title for ws in wb.worksheets}
    if base not in existing:
        return base
    n = 1
    while True:
        suffix = f" ({n})"
        candidate = base[: _MAX_SHEET_NAME - len(suffix)] + suffix
        if candidate not in existing:
            return candidate
        n += 1


def _autosize(ws: Worksheet, max_width: int = 40) -> None:
    widths: dict[int, int] = {}
    for row in ws.iter_rows():
        for cell in row:
            if cell.value is None:
                continue
            col = cell.column
            assert col is not None
            widths[col] = max(widths.get(col, 0), len(str(cell.value)))
    for col, width in widths.items():
        ws.column_dimensions[get_column_letter(col)].width = min(max_width, width + 2)


def _header_row(ws: Worksheet, values: list[str]) -> None:
    ws.append(values)
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL


def _write_header_at(ws: Worksheet, row_idx: int, values: list[str]) -> None:
    """Like `_header_row`, but for a header that doesn't start at row 1 — the
    division and rooms sheets stack several day sections in one sheet."""
    for c_idx, value in enumerate(values, start=1):
        cell = ws.cell(row=row_idx, column=c_idx, value=value)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL


def _natural_key(text: str) -> list[object]:
    """Sort key that reads embedded numbers as numbers ("Room 9" < "Room 10")."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", text)]


def _day_header_label(day: Date) -> str:
    """ "Thursday, 17 September" — the day-block header above a division's or
    the rooms overview's per-day section."""
    return f"{day.strftime('%A')}, {day.day} {day.strftime('%B')}"


def _panel_has_assignments(view: RoomView, panel_id: str) -> bool:
    """Whether this room actually hosted a real interview for `panel_id` on
    this day. A room/panel pairing exists in `panels.yaml` independent of
    whether the solver ever placed anyone there that specific day — rendering
    it anyway produces a dead, empty room-panel column (bug: Room 3013 showing
    up on a day Logistics never used it)."""
    return any(row.cells.get(panel_id) is not None for row in view.rows)


def _build_division_blocks(
    room_views: Sequence[RoomView],
) -> dict[str, dict[Date, list[tuple[str, str, RoomView]]]]:
    """division -> day -> [(room_id, panel_id, that room's day view), ...],
    rooms sorted naturally within each day.

    Regroups the same per-room-per-day views the timetable is built from
    (CLAUDE.md invariant 2: the solver's own panel/division data, nothing
    guessed) so a division reads as one sheet with a block per day, the
    committee's manual scheduling layout. A room appears once per division
    it hosts that day — room-exclusivity means a room never runs two panels
    of the same division at once, so this can't double up a room. A room/panel
    that never actually held an interview that day (static panel-room config
    with zero real assignments) is dropped rather than rendered as an empty
    column — the room-panel count per day always reflects the real run data."""
    blocks: dict[str, dict[Date, list[tuple[str, str, RoomView]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for view in room_views:
        for panel_id, division in view.panel_divisions.items():
            if not _panel_has_assignments(view, panel_id):
                continue
            blocks[division.value][view.date].append((view.room_id, panel_id, view))
    for by_day in blocks.values():
        for rooms in by_day.values():
            rooms.sort(key=lambda t: _natural_key(t[0]))
    return blocks


# Sub-divisions that are just the parent division's own name spelled out (the
# applicant's raw form text, e.g. "Logistics" for the LOGISTICS division, or
# the legacy short label "Creative" for CREATIVE) carry no disambiguating
# information — showing them is the redundant "(Logistics)" suffix from
# bug 5. A qualifier that actually distinguishes sub-divisions under one
# parent (e.g. "Media Marketing" vs "Documentation" under MEDMARDOC, or
# "WebMaster" vs "Design and Decor" under CREATIVE) is always kept.
_REDUNDANT_SUB_DIVISION_LABELS: dict[str, frozenset[str]] = {
    "LOGISTICS": frozenset({"logistics"}),
    "LIAISON": frozenset({"liaison"}),
    "PROGRAM": frozenset({"program"}),
    "FNB": frozenset({"finance and booth", "finance & booth"}),
    "CREATIVE": frozenset({"creative", "creative and decor"}),
    "MEDMARDOC": frozenset({"media marketing and documentation"}),
}
_PARENTHETICAL = re.compile(r"^.*\((.+)\)\s*$")


def _sub_division_qualifier(sub_division: str, division: str) -> str | None:
    """The meaningful part of `sub_division` to show alongside an applicant's
    name on their own division's tab, or `None` if it would just repeat the
    tab's own division (bug 5). Raw form text often carries the parent name
    as a prefix — "Creative and Decor (WebMaster)" — so the parenthetical, if
    present, is what actually disambiguates."""
    match = _PARENTHETICAL.match(sub_division.strip())
    text = match.group(1).strip() if match else sub_division.strip()
    if text.casefold() in _REDUNDANT_SUB_DIVISION_LABELS.get(division, frozenset()):
        return None
    return text or None


def _write_division_sheet(
    wb: Workbook,
    division: str,
    day_blocks: dict[Date, list[tuple[str, str, RoomView]]],
) -> None:
    """One division's sheet: a day header row, then that day's rooms as
    [Interviewer 1 | Interviewer 2 | Room N] column groups sharing one Time
    column, one such block per day in date order, sized to exactly the rooms
    that division actually used that day (never a fixed count — see
    `_build_division_blocks`).

    "Interviewer 1"/"Interviewer 2" are blank placeholder columns for the
    committee to fill in by hand — the data model has no interviewer names to
    put there. They get the same bordered, header-styled treatment as the
    reference sheet; a real data-validation dropdown would need a source list
    of interviewer names the system doesn't track, so that's left as a
    follow-up rather than a partial/fake dropdown."""
    ws = wb.create_sheet(_unique_sheet_name(wb, division))
    row_idx = 1
    for day in sorted(day_blocks):
        rooms = day_blocks[day]
        width = 1 + 3 * len(rooms)

        day_cell = ws.cell(row=row_idx, column=1, value=_day_header_label(day))
        day_cell.font = HEADER_FONT
        day_cell.fill = HEADER_FILL
        if width > 1:
            ws.merge_cells(start_row=row_idx, start_column=1, end_row=row_idx, end_column=width)
        row_idx += 1

        header_row_idx = row_idx
        headers = ["Time"]
        for room_id, _panel_id, _view in rooms:
            headers += ["Interviewer 1", "Interviewer 2", f"Room {room_id}"]
        _write_header_at(ws, header_row_idx, headers)
        # One background colour per room group, cycled, so the header (and the
        # blank Interviewer columns under it) reads as one visual unit.
        for room_idx, _room in enumerate(rooms):
            fill = ROOM_HEADER_FILLS[room_idx % len(ROOM_HEADER_FILLS)]
            for col in range(2 + room_idx * 3, 5 + room_idx * 3):
                ws.cell(row=header_row_idx, column=col).fill = fill
        row_idx += 1

        # Every room shares this day's slot axis (build_room_views derives it
        # from the same grid), so any room's row list gives the time column.
        slot_axis = rooms[0][2].rows
        rows_by_room = [{r.slot_id: r for r in view.rows} for _rid, _pid, view in rooms]

        for slot in slot_axis:
            ws.cell(
                row=row_idx,
                column=1,
                value=f"{slot.start_time.strftime('%H:%M')}-{slot.end_time.strftime('%H:%M')}",
            )
            col = 2
            for (_room_id, panel_id, _view), by_slot in zip(rooms, rows_by_room, strict=True):
                ws.cell(row=row_idx, column=col).border = BLANK_COLUMN_BORDER
                ws.cell(row=row_idx, column=col + 1).border = BLANK_COLUMN_BORDER
                slot_row = by_slot.get(slot.slot_id)
                occupant = slot_row.cells.get(panel_id) if slot_row is not None else None
                if occupant is not None:
                    qualifier = _sub_division_qualifier(occupant.sub_division, division)
                    name = (
                        f"{occupant.full_name} ({qualifier})"
                        if qualifier is not None
                        else occupant.full_name
                    )
                    target = ws.cell(row=row_idx, column=col + 2, value=name)
                    if occupant.is_clash:
                        target.fill = RED_FILL
                        target.font = RED_FONT
                    elif occupant.is_locked:
                        target.font = LOCKED_FONT
                col += 3
            row_idx += 1

        row_idx += 1  # blank row between day blocks
    _autosize(ws)


def write_xlsx(path: Path, room_views: Sequence[RoomView]) -> None:
    """Write the schedule workbook: exactly one sheet per top-level division —
    whatever divisions `config/divisions.yaml` actually declares, never a
    hardcoded count — each day's rooms as a block, Thursday above Friday, the
    committee's manual scheduling layout (FR-50, FR-54).

    The Applicants tab, per-panel running orders and the conflicts report are
    published separately (`write_applicants_xlsx`, the panel-view HTML, and
    `conflicts.csv`) — duplicating them into this file was the source of the
    extra "Panel <id>" tabs bug, so schedule.xlsx carries division sheets only."""
    wb = Workbook()
    default_sheet = wb.active
    assert default_sheet is not None
    wb.remove(default_sheet)

    division_blocks = _build_division_blocks(room_views)
    for division in sorted(division_blocks):
        _write_division_sheet(wb, division, division_blocks[division])

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


# The web app's Applicants tab, column for column as it renders (FR-51): a
# leading row number, the applicant's declared day/time preference, then their
# two interviews in slot-time order. "Div 1"/"Div 2" are the chronological
# first/second interview (whichever slot is earlier), not the form-choice
# order — build_applicant_view already places them that way.
APPLICANTS_TAB_HEADER = [
    "#",
    "Name",
    "Preference",
    "Div 1",
    "Time 1",
    "Room 1",
    "Div 2",
    "Time 2",
    "Room 2",
    "Clash",
]
# 1-indexed column ranges for each interview's block, for clash highlighting.
_TAB_CHOICE1_COLS = range(4, 7)
_TAB_CHOICE2_COLS = range(7, 10)
_TAB_CLASH_COL = 10


def _tab_when(choice: ApplicantChoiceView | None) -> str:
    """ "Thu 17 Sep 09:00" — the tab's date + start-time cell (`formatDate` +
    `formatTime` in the frontend)."""
    if choice is None:
        return ""
    d = choice.date
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')} {choice.start_time.strftime('%H:%M')}"


def _tab_room(choice: ApplicantChoiceView | None) -> str:
    """Room id, with the tab's 🔒 marker when the interview is locked."""
    if choice is None:
        return ""
    return f"{choice.room} 🔒" if choice.is_locked else choice.room


def write_applicants_xlsx(
    path: Path,
    rows: Sequence[ApplicantViewRow],
    preferences: dict[str, str],
) -> None:
    """Write a standalone one-sheet workbook mirroring the web app's
    Applicants tab exactly as displayed (FR-51): row number, name, declared
    preference, then each interview's sub-division / date+time / room in
    slot-time order, and a clash flag. Rows are ordered by name like the tab;
    clash cells are shaded red (FR-54).

    ``preferences`` maps ``applicant_id`` -> the declared-availability string
    shown in the Preference column; a missing entry renders blank.
    """
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = _sheet_name("Applicants")
    _header_row(ws, APPLICANTS_TAB_HEADER)

    ordered = sorted(rows, key=lambda r: (r.full_name.casefold(), r.applicant_id))
    for n, row in enumerate(ordered, start=1):
        ws.append(
            [
                n,
                row.full_name,
                preferences.get(row.applicant_id, ""),
                row.choice1.sub_division if row.choice1 is not None else "",
                _tab_when(row.choice1),
                _tab_room(row.choice1),
                row.choice2.sub_division if row.choice2 is not None else "",
                _tab_when(row.choice2),
                _tab_room(row.choice2),
                "CLASH" if row.has_clash else "",
            ]
        )

    for r_idx, row in enumerate(ordered, start=2):
        blocks = []
        if row.choice1 is not None and row.choice1.is_clash:
            blocks.append(_TAB_CHOICE1_COLS)
        if row.choice2 is not None and row.choice2.is_clash:
            blocks.append(_TAB_CHOICE2_COLS)
        if row.has_clash:
            blocks.append(range(_TAB_CLASH_COL, _TAB_CLASH_COL + 1))
        for cols in blocks:
            for col in cols:
                cell = ws.cell(row=r_idx, column=col)
                cell.fill = RED_FILL
                cell.font = RED_FONT
    _autosize(ws)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


_ROOMS_HEADER = ["Room", "Divisions running"]


_RoomsByDay = dict[Date, dict[str, list[tuple[str, str]]]]


def _rooms_by_day(assignments: Sequence[Assignment]) -> _RoomsByDay:
    """day -> room -> sorted [(division, panel_id), ...] actually interviewing
    there that day, straight from the run's own assignments (CLAUDE.md
    invariant 3 — nothing guessed from a room/panel's static configuration)."""
    grouped: dict[Date, dict[str, set[tuple[str, str]]]] = defaultdict(lambda: defaultdict(set))
    for a in assignments:
        grouped[a.date][a.room].add((a.division.value, a.panel_id))
    return {
        day: {room: sorted(pairs) for room, pairs in rooms.items()}
        for day, rooms in grouped.items()
    }


def write_rooms_xlsx(
    path: Path,
    assignments: Sequence[Assignment],
    rooms: Sequence[Room] | None = None,
) -> None:
    """Write a room-oriented overview, one section per day: every room that
    has an interview that day, and which panels/divisions are running there.

    A quick-scan reference for someone walking the venue — "what's happening
    in this room today" — not a duplicate of the per-division timetables, so
    it deliberately carries only room + division/panel, no times or names.

    `rooms` (optional, e.g. `resolve_rooms(settings.rooms, grid)`) adds one row
    per day for every room outside the interview pool (`interview_room=False`,
    e.g. a waiting room) so it still appears — labelled, with an empty summary
    — instead of silently vanishing because it never has an assignment.
    """
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = _sheet_name("Rooms")

    by_day = _rooms_by_day(assignments)
    landmark_rooms = [r for r in (rooms or []) if not r.interview_room]
    all_days = set(by_day) | {day for r in landmark_rooms for day in r.days}

    row_idx = 1
    for day in sorted(all_days):
        day_cell = ws.cell(row=row_idx, column=1, value=_day_header_label(day))
        day_cell.font = HEADER_FONT
        day_cell.fill = HEADER_FILL
        ws.merge_cells(
            start_row=row_idx, start_column=1, end_row=row_idx, end_column=len(_ROOMS_HEADER)
        )
        row_idx += 1

        _write_header_at(ws, row_idx, _ROOMS_HEADER)
        row_idx += 1

        room_rows = dict(by_day.get(day, {}))
        labels = {room_id: room_id for room_id in room_rows}
        for room in landmark_rooms:
            if day in room.days:
                room_rows.setdefault(room.id, [])
                labels[room.id] = room.label or room.id
        for room_id in sorted(room_rows, key=_natural_key):
            summary = ", ".join(
                f"{division} ({panel_id})" for division, panel_id in room_rows[room_id]
            )
            ws.cell(row=row_idx, column=1, value=labels[room_id])
            ws.cell(row=row_idx, column=2, value=summary or "— Waiting Room, no interviews —")
            row_idx += 1

        row_idx += 1  # blank row between days

    _autosize(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
