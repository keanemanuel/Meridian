"""XLSX export shaping: the schedule workbook's per-division/day layout, the
rooms overview, and the standalone Applicants workbook that mirrors the web
app's Applicants tab (FR-50, FR-51, FR-53, FR-54)."""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

from openpyxl import load_workbook

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.models import Assignment, Room
from iff_scheduler.export.applicant_view import ApplicantChoiceView, ApplicantViewRow
from iff_scheduler.export.room_view import RoomView, RoomViewCell, RoomViewRow
from iff_scheduler.export.xlsx_writer import (
    APPLICANTS_TAB_HEADER,
    _sub_division_qualifier,
    write_applicants_xlsx,
    write_rooms_xlsx,
    write_xlsx,
)

THU = date(2026, 9, 17)
FRI = date(2026, 9, 18)

_SLOT_A = ("2026-09-17_0900", time(9, 0), time(9, 20))
_SLOT_B = ("2026-09-17_0920", time(9, 20), time(9, 40))


def _cell(name: str, sub: str, *, division: DivisionCode, is_clash: bool = False) -> RoomViewCell:
    return RoomViewCell(
        applicant_id=name,
        full_name=name,
        sub_division=sub,
        division=division,
        choice_index=1,
        is_clash=is_clash,
        is_locked=False,
    )


def _room_view(
    room_id: str,
    day: date,
    panel_id: str,
    division: DivisionCode,
    occupants: dict[str, RoomViewCell | None],
) -> RoomView:
    """A room's day, both grid slots, `occupants` keyed by slot_id (default
    empty)."""
    rows = [
        RoomViewRow(
            slot_id=slot_id,
            start_time=start,
            end_time=end,
            cells={panel_id: occupants.get(slot_id)},
        )
        for slot_id, start, end in (_SLOT_A, _SLOT_B)
    ]
    return RoomView(
        room_id=room_id,
        date=day,
        day_label="Thu" if day == THU else "Fri",
        panel_ids=[panel_id],
        panel_divisions={panel_id: division},
        rows=rows,
    )


def test_schedule_has_one_sheet_per_division_sorted(tmp_path: Path) -> None:
    """Rooms with no actual occupants (both here) still surface their division
    — the sheet-count fix (bug 1) is about dropping extra Panel/Applicants/
    Conflicts tabs, not about a division needing occupied rows to appear."""
    views = [
        _room_view(
            "2030",
            THU,
            "LOGISTICS-1",
            DivisionCode.LOGISTICS,
            {_SLOT_A[0]: _cell("A", "Logistics", division=DivisionCode.LOGISTICS)},
        ),
        _room_view(
            "2014",
            THU,
            "CREATIVE-1",
            DivisionCode.CREATIVE,
            {_SLOT_A[0]: _cell("B", "Creative", division=DivisionCode.CREATIVE)},
        ),
    ]
    out = tmp_path / "schedule.xlsx"
    write_xlsx(out, views)
    assert load_workbook(out).sheetnames[:2] == ["CREATIVE", "LOGISTICS"]


def test_schedule_has_exactly_one_sheet_per_top_level_division(tmp_path: Path) -> None:
    """Bug 1: schedule.xlsx must contain exactly one sheet per top-level
    division that config/divisions.yaml declares — no extra "Panel <id>",
    Applicants or Conflicts tabs, and never a hardcoded division count."""
    from iff_scheduler.settings import load_settings

    top_level_divisions = {d.code for d in load_settings().divisions.divisions}

    views = [
        _room_view(
            f"room-{division.value}",
            THU,
            f"{division.value}-1",
            division,
            {_SLOT_A[0]: _cell("Applicant", "Sub", division=division)},
        )
        for division in DivisionCode
    ]
    out = tmp_path / "schedule.xlsx"
    write_xlsx(out, views)

    sheetnames = load_workbook(out).sheetnames
    assert len(sheetnames) == len(top_level_divisions)
    assert set(sheetnames) == {d.value for d in top_level_divisions}
    assert not any(name.startswith("Panel ") for name in sheetnames)
    assert "Applicants" not in sheetnames
    assert "Conflicts" not in sheetnames


def test_division_sheet_omits_a_room_with_no_real_assignments_that_day(tmp_path: Path) -> None:
    """Bug 2: a room configured for a division but with zero actual
    assignments on a given day (e.g. Room 3013 idle on Thursday while Room
    2019 is used) must not render as an empty room-panel column."""
    views = [
        _room_view(
            "2019",
            THU,
            "LOGISTICS-1",
            DivisionCode.LOGISTICS,
            {_SLOT_A[0]: _cell("Amy", "Logistics", division=DivisionCode.LOGISTICS)},
        ),
        _room_view("3013", THU, "LOGISTICS-2", DivisionCode.LOGISTICS, {}),  # never occupied
        _room_view(
            "3013",
            FRI,
            "LOGISTICS-2",
            DivisionCode.LOGISTICS,
            {_SLOT_A[0]: _cell("Bo", "Logistics", division=DivisionCode.LOGISTICS)},
        ),
        _room_view("2019", FRI, "LOGISTICS-1", DivisionCode.LOGISTICS, {}),  # never occupied
    ]
    out = tmp_path / "schedule.xlsx"
    write_xlsx(out, views)
    ws = load_workbook(out)["LOGISTICS"]

    thu_headers = [c.value for c in ws[2]]
    assert thu_headers == ["Time", "Interviewer 1", "Interviewer 2", "Room 2019"]
    assert "Room 3013" not in thu_headers

    fri_header_row = next(
        r
        for r in range(1, ws.max_row + 1)
        if ws.cell(row=r, column=1).value == "Friday, 18 September"
    )
    fri_headers = [c.value for c in ws[fri_header_row + 1]]
    assert fri_headers == ["Time", "Interviewer 1", "Interviewer 2", "Room 3013"]
    assert "Room 2019" not in fri_headers


def test_sub_division_qualifier_drops_the_redundant_parent_name() -> None:
    """Bug 5: on a division's own tab, a sub-division that just repeats the
    division's own name (raw form text or the legacy short label) adds
    nothing and is dropped."""
    assert _sub_division_qualifier("Logistics", "LOGISTICS") is None
    assert _sub_division_qualifier("Program", "PROGRAM") is None
    assert _sub_division_qualifier("Liaison", "LIAISON") is None
    assert _sub_division_qualifier("Finance and Booth", "FNB") is None
    assert _sub_division_qualifier("Creative", "CREATIVE") is None


def test_sub_division_qualifier_keeps_a_meaningful_distinction() -> None:
    """A sub-division that actually distinguishes roles under one parent is
    kept — matching the app's own short-label convention (CLAUDE.md:
    "(Media Marketing)" vs "(Documentation)")."""
    assert (
        _sub_division_qualifier(
            "Media Marketing and Documentation (Media Marketing)", "MEDMARDOC"
        )
        == "Media Marketing"
    )
    assert (
        _sub_division_qualifier(
            "Media Marketing and Documentation (Documentation)", "MEDMARDOC"
        )
        == "Documentation"
    )
    assert _sub_division_qualifier("Creative and Decor (WebMaster)", "CREATIVE") == "WebMaster"
    assert _sub_division_qualifier("WebMaster", "CREATIVE") == "WebMaster"


def test_division_sheet_lays_out_rooms_side_by_side_with_day_blocks(tmp_path: Path) -> None:
    """Two rooms run CREATIVE on Thursday (side by side); Friday adds a
    second day block below with a different room count of its own (FR-50)."""
    slot_a_id = _SLOT_A[0]
    views = [
        _room_view(
            "2014",
            THU,
            "CREATIVE-1",
            DivisionCode.CREATIVE,
            {slot_a_id: _cell("Amy", "Creative", division=DivisionCode.CREATIVE)},
        ),
        _room_view(
            "2018",
            THU,
            "CREATIVE-2",
            DivisionCode.CREATIVE,
            {slot_a_id: _cell("Bo", "Creative", division=DivisionCode.CREATIVE, is_clash=True)},
        ),
        _room_view(
            "2014",
            FRI,
            "CREATIVE-1",
            DivisionCode.CREATIVE,
            {slot_a_id: _cell("Cara", "WebMaster", division=DivisionCode.CREATIVE)},
        ),
    ]
    out = tmp_path / "schedule.xlsx"
    write_xlsx(out, views)
    ws = load_workbook(out)["CREATIVE"]

    # Row 1: Thursday's day header, merged across the two rooms' 7 columns.
    assert ws.cell(row=1, column=1).value == "Thursday, 17 September"
    assert any(
        rng.min_row == 1 and rng.max_row == 1 and rng.min_col == 1 and rng.max_col == 7
        for rng in ws.merged_cells.ranges
    )

    # Row 2: Time, then each room's [Interviewer 1 | Interviewer 2 | Room N].
    assert [c.value for c in ws[2]][:7] == [
        "Time",
        "Interviewer 1",
        "Interviewer 2",
        "Room 2014",
        "Interviewer 1",
        "Interviewer 2",
        "Room 2018",
    ]
    # Each room's header group carries its own distinct background colour.
    room1_fill = ws.cell(row=2, column=4).fill.start_color.rgb
    room2_fill = ws.cell(row=2, column=7).fill.start_color.rgb
    assert room1_fill != room2_fill

    # Row 3: the 09:00 slot — Amy in room 2014's column, Bo (clashing) in 2018's.
    # Bug 5: sub-division "Creative" duplicates the CREATIVE tab's own division
    # name, so no "(Creative)" suffix is appended.
    assert ws.cell(row=3, column=1).value == "09:00-09:20"
    assert ws.cell(row=3, column=4).value == "Amy"
    assert ws.cell(row=3, column=7).value == "Bo"
    assert ws.cell(row=3, column=7).font.color.rgb == "009C0006"  # RED_FONT, round-tripped
    assert ws.cell(row=3, column=2).value is None  # Interviewer 1: left blank
    assert ws.cell(row=3, column=2).border.left.style == "thin"  # blank but bordered

    # Row 4: the 09:20 slot — nobody in either room.
    assert ws.cell(row=4, column=4).value is None
    assert ws.cell(row=4, column=7).value is None

    # Friday's block starts below a blank spacer row, with its own single-room
    # header — independently sized from Thursday's two rooms.
    friday_header_row = next(
        r
        for r in range(1, ws.max_row + 1)
        if ws.cell(row=r, column=1).value == "Friday, 18 September"
    )
    assert [c.value for c in ws[friday_header_row + 1]][:4] == [
        "Time",
        "Interviewer 1",
        "Interviewer 2",
        "Room 2014",
    ]
    # Bug 5: a meaningful sub-division qualifier (distinguishing WebMaster from
    # Design and Decor under CREATIVE) is kept.
    assert ws.cell(row=friday_header_row + 2, column=4).value == "Cara (WebMaster)"


def test_rooms_xlsx_groups_panels_by_room_and_day(tmp_path: Path) -> None:
    def assignment(day: date, room: str, division: DivisionCode, panel_id: str) -> Assignment:
        return Assignment(
            applicant_id="A",
            full_name="A",
            email="a@example.com",
            choice_index=1,
            sub_division="Sub",
            division=division,
            panel_id=panel_id,
            room=room,
            slot_id=f"{day.isoformat()}_0900",
            date=day,
            start_time=time(9, 0),
            end_time=time(9, 20),
            is_clash=False,
            is_locked=False,
            same_parent_pair=False,
        )

    assignments = [
        assignment(THU, "2016", DivisionCode.PROGRAM, "PROGRAM-A1"),
        assignment(THU, "2016", DivisionCode.LIAISON, "LIAISON-A2"),
        assignment(THU, "2018", DivisionCode.CREATIVE, "CREATIVE-A1"),
        assignment(FRI, "2016", DivisionCode.FNB, "FNB-A1"),
    ]
    out = tmp_path / "rooms.xlsx"
    write_rooms_xlsx(out, assignments)
    ws = load_workbook(out).active

    assert ws.cell(row=1, column=1).value == "Thursday, 17 September"
    assert [c.value for c in ws[2]][:2] == ["Room", "Divisions running"]
    assert ws.cell(row=3, column=1).value == "2016"
    assert ws.cell(row=3, column=2).value == "LIAISON (LIAISON-A2), PROGRAM (PROGRAM-A1)"
    assert ws.cell(row=4, column=1).value == "2018"
    assert ws.cell(row=4, column=2).value == "CREATIVE (CREATIVE-A1)"

    friday_header_row = next(
        r
        for r in range(1, ws.max_row + 1)
        if ws.cell(row=r, column=1).value == "Friday, 18 September"
    )
    assert ws.cell(row=friday_header_row + 2, column=1).value == "2016"
    assert ws.cell(row=friday_header_row + 2, column=2).value == "FNB (FNB-A1)"


def test_rooms_xlsx_shows_a_waiting_room_as_a_landmark_with_no_schedule(tmp_path: Path) -> None:
    """A room outside the interview pool (`interview_room=False`, e.g. Room
    2018 converted to a waiting room) still gets a row on every day it's open
    — labelled, with an empty summary — instead of silently vanishing because
    it never has an assignment."""
    waiting_room = Room(
        id="2018",
        max_concurrent_panels=1,
        divisions=[],
        days=[THU, FRI],
        interview_room=False,
        label="Waiting Room",
    )
    assignments = [
        Assignment(
            applicant_id="A",
            full_name="A",
            email="a@example.com",
            choice_index=1,
            sub_division="Sub",
            division=DivisionCode.PROGRAM,
            panel_id="PROGRAM-A1",
            room="2016",
            slot_id=f"{THU.isoformat()}_0900",
            date=THU,
            start_time=time(9, 0),
            end_time=time(9, 20),
            is_clash=False,
            is_locked=False,
            same_parent_pair=False,
        )
    ]
    out = tmp_path / "rooms.xlsx"
    write_rooms_xlsx(out, assignments, [waiting_room])
    ws = load_workbook(out).active

    thursday_ids = {
        ws.cell(row=r, column=1).value
        for r in range(3, ws.max_row + 1)
        if ws.cell(row=r, column=1).value not in (None, "Friday, 18 September")
    }
    assert "Waiting Room" in thursday_ids
    waiting_row = next(
        r
        for r in range(1, ws.max_row + 1)
        if ws.cell(row=r, column=1).value == "Waiting Room"
    )
    assert ws.cell(row=waiting_row, column=2).value == "— Waiting Room, no interviews —"


def test_build_room_views_adds_an_empty_landmark_for_a_waiting_room() -> None:
    """`build_room_views` emits one panel-less, row-less `RoomView` per day for
    a room with `interview_room=False`, so it still surfaces in room-oriented
    exports (Room View / rooms.xlsx) without ever carrying a schedule."""
    from iff_scheduler.domain.models import Slot
    from iff_scheduler.export.room_view import build_room_views

    waiting_room = Room(
        id="2018",
        max_concurrent_panels=1,
        divisions=[],
        days=[THU, FRI],
        interview_room=False,
        label="Waiting Room",
    )
    slots = [
        Slot(
            slot_id=f"{THU.isoformat()}_0900",
            date=THU,
            day_label="Thu",
            start_time=time(9, 0),
            end_time=time(9, 20),
            slot_index=0,
        ),
        Slot(
            slot_id=f"{FRI.isoformat()}_0900",
            date=FRI,
            day_label="Fri",
            start_time=time(9, 0),
            end_time=time(9, 20),
            slot_index=0,
        ),
    ]

    views = build_room_views(assignments=[], panels=[], rooms=[waiting_room], slots=slots)

    assert len(views) == 2
    assert {v.date for v in views} == {THU, FRI}
    for view in views:
        assert view.room_id == "2018"
        assert view.room_label == "Waiting Room"
        assert view.interview_room is False
        assert view.panel_ids == []
        assert view.rows == []


def _choice(
    division: DivisionCode,
    sub: str,
    day: date,
    start: time,
    *,
    room: str = "2014",
    is_clash: bool = False,
    is_locked: bool = False,
) -> ApplicantChoiceView:
    end_minutes = start.hour * 60 + start.minute + 20
    return ApplicantChoiceView(
        division=division,
        sub_division=sub,
        panel_id=f"{division.value}-1",
        room=room,
        date=day,
        start_time=start,
        end_time=time(end_minutes // 60, end_minutes % 60),
        is_clash=is_clash,
        is_locked=is_locked,
    )


def test_applicants_workbook_mirrors_the_tab(tmp_path: Path) -> None:
    rows = [
        ApplicantViewRow(
            applicant_id="A-002",
            full_name="Zoe Adams",
            email="zoe@example.com",
            choice1=_choice(DivisionCode.CREATIVE, "Creative", THU, time(9, 0), room="2014"),
            choice2=_choice(DivisionCode.PROGRAM, "WebMaster", FRI, time(10, 20), room="2050"),
        ),
        ApplicantViewRow(
            applicant_id="A-001",
            full_name="amy brown",
            email="amy@example.com",
            choice1=_choice(DivisionCode.LOGISTICS, "Logistics", THU, time(9, 20), is_clash=True),
            choice2=None,
        ),
    ]
    preferences = {"A-001": "Thu 17 Sep", "A-002": "Thu 17 Sep; Fri 18 Sep"}

    out = tmp_path / "applicants.xlsx"
    write_applicants_xlsx(out, rows, preferences)

    ws = load_workbook(out).active
    assert [c.value for c in ws[1]] == APPLICANTS_TAB_HEADER

    # Sorted by name (case-insensitive), so "amy brown" (#1) precedes "Zoe Adams" (#2).
    # openpyxl reads an empty string cell back as None.
    body = [[("" if c.value is None else c.value) for c in row] for row in ws.iter_rows(min_row=2)]
    assert body[0][:3] == [1, "amy brown", "Thu 17 Sep"]
    assert body[0][3:6] == ["Logistics", "Thu 17 Sep 09:20", "2014"]
    assert body[0][6:9] == ["", "", ""]  # single-choice applicant: blank Div 2 block
    assert body[0][9] == "CLASH"

    assert body[1][:3] == [2, "Zoe Adams", "Thu 17 Sep; Fri 18 Sep"]
    assert body[1][4] == "Thu 17 Sep 09:00"
    assert body[1][7] == "Fri 18 Sep 10:20"
    assert body[1][9] == ""


def test_applicants_workbook_marks_locked_room(tmp_path: Path) -> None:
    rows = [
        ApplicantViewRow(
            applicant_id="A-1",
            full_name="Pat Lee",
            email="pat@example.com",
            choice1=_choice(DivisionCode.FNB, "FNB", THU, time(9, 0), is_locked=True),
            choice2=_choice(DivisionCode.CREATIVE, "Creative", THU, time(9, 40)),
        )
    ]
    out = tmp_path / "applicants.xlsx"
    write_applicants_xlsx(out, rows, {})

    ws = load_workbook(out).active
    assert ws.cell(row=2, column=6).value == "2014 \U0001f512"
    assert ws.cell(row=2, column=9).value == "2014"
