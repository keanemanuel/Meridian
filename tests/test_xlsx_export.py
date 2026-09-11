"""XLSX export shaping: the schedule workbook's sheet order and the
standalone Applicants workbook that mirrors the web app's Applicants tab
(FR-50, FR-51, FR-53, FR-54)."""

from __future__ import annotations

from datetime import date, time
from pathlib import Path

from openpyxl import load_workbook

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.export.applicant_view import ApplicantChoiceView, ApplicantViewRow
from iff_scheduler.export.room_view import RoomView
from iff_scheduler.export.xlsx_writer import (
    APPLICANTS_TAB_HEADER,
    write_applicants_xlsx,
    write_xlsx,
)

THU = date(2026, 9, 17)
FRI = date(2026, 9, 18)


def _room_view(room_id: str, day: date, division: DivisionCode) -> RoomView:
    panel_id = f"{division.value}-{room_id}"
    return RoomView(
        room_id=room_id,
        date=day,
        day_label="Thu" if day == THU else "Fri",
        panel_ids=[panel_id],
        panel_divisions={panel_id: division},
        rows=[],
    )


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


def test_schedule_room_sheets_ordered_day_then_division(tmp_path: Path) -> None:
    """Room/day sheets come out Day-major (Thursday before Friday), then
    division alphabetically within a day (FR-50)."""
    # Deliberately shuffled input.
    views = [
        _room_view("2050", FRI, DivisionCode.PROGRAM),
        _room_view("2014", THU, DivisionCode.LOGISTICS),
        _room_view("2014", FRI, DivisionCode.CREATIVE),
        _room_view("2030", THU, DivisionCode.CREATIVE),
    ]
    out = tmp_path / "schedule.xlsx"
    write_xlsx(out, views, [], [], [])

    sheet_names = load_workbook(out).sheetnames
    # Thursday's two sheets first (CREATIVE before LOGISTICS), then Friday's
    # (CREATIVE before PROGRAM).
    assert sheet_names[:4] == [
        "2030 Thu",
        "2014 Thu",
        "2014 Fri",
        "2050 Fri",
    ]


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
