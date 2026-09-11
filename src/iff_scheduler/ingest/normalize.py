"""Dedupe, sub-division -> parent mapping, availability parsing (FR-02, FR-03,
FR-05, FR-04b; SPEC.md §12 E-01, E-04, E-05).

Nothing here rejects a row outright — an unmapped sub-division becomes a
`None` division, a bad timestamp becomes a `None` submitted_at, an applicant
with no overlapping availability gets an empty slot list. `validate.py` is
the single place that turns those into rejections, so the reasons stay in
one report (CLAUDE.md invariant 3: "nothing is guessed").

Raw column names are the headers of the real IFF recruitment Google Form
(see `COLUMN_*` below). Availability comes from a single "Preferred Interview
Date" column whose cells name a day ("Thursday, 18 September 2025"), not time
blocks — `parse_preferred_dates` turns each named weekday that matches an
event day into that whole day's window, so an interview may land anywhere in
it. Matching is by weekday name, not the literal date: the form's calendar
year need not equal the configured event year, and nothing is inferred
beyond the day the applicant actually chose (CLAUDE.md invariant 3).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from datetime import time as Time
from typing import Literal

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import SlotGrid
from iff_scheduler.settings import DivisionsConfig, EventConfig, canonical_sub_division

COLUMN_TIMESTAMP = "Timestamp"
COLUMN_EMAIL = "Email Address"
COLUMN_FULL_NAME = "Full Name"
COLUMN_PHONE = "Phone Number (WhatsApp)"
COLUMN_STUDENT_ID = "Student ID"
COLUMN_SUBDIVISION_1 = "First Preference"
COLUMN_SUBDIVISION_2 = "Second Preference"
COLUMN_PREFERRED_DATE = "Preferred Interview Date"
# The real form has no free-text scheduling-notes field; kept so an augmented
# export that adds one still flows through (`.get` returns None otherwise).
COLUMN_NOTES = "Accessibility / scheduling notes"

# Headers the ingest actually needs, each with the spellings seen in real
# exports. Everything else the form emits — University, Major, State of
# Degree, Proof of Student Enrolment, "Current Location (e.g.: CBD, etc)",
# Social Media Accounts, the essay, CV / Google Drive links, Required Files,
# Email Confirmation, and trailing filler like "Column 1" — is read and
# ignored. Unknown columns must never cause a rejection: a committee member
# adding a question to the form should not empty the schedule.
#
# Matching is on the folded header (lower-cased, whitespace collapsed, and
# any trailing "(...)" hint dropped), so "Current Location (e.g.: CBD, etc)"
# with its embedded comma, or "Email address", still resolve.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    COLUMN_TIMESTAMP: ("timestamp", "submitted at", "submission time"),
    COLUMN_EMAIL: ("email address", "email", "e-mail address", "email addresses"),
    COLUMN_FULL_NAME: ("full name", "name", "nama"),
    COLUMN_PHONE: ("phone number", "phone", "whatsapp", "whatsapp number", "contact number"),
    COLUMN_STUDENT_ID: ("student id", "student number"),
    COLUMN_SUBDIVISION_1: ("first preference", "1st preference", "first choice"),
    COLUMN_SUBDIVISION_2: ("second preference", "2nd preference", "second choice"),
    COLUMN_PREFERRED_DATE: (
        "preferred interview date",
        "preferred interview dates",
        "interview date",
        "availability",
    ),
    COLUMN_NOTES: (
        "accessibility / scheduling notes",
        "accessibility notes",
        "scheduling notes",
        "notes",
    ),
}

_PARENTHETICAL_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")


def fold_header(raw: str) -> str:
    """Fold a CSV header to its comparison key.

    Lower-cases, collapses whitespace and drops one trailing parenthetical
    hint, so "Phone Number (WhatsApp)" folds to "phone number" and
    "Current Location (e.g.: CBD, etc)" to "current location".
    """
    collapsed = " ".join((raw or "").split())
    return _PARENTHETICAL_SUFFIX.sub("", collapsed).strip().lower()


def resolve_columns(raw: Mapping[str, str]) -> dict[str, str]:
    """Map each logical column name to this row's value, via COLUMN_ALIASES.

    An exact header match always wins; the folded aliases are the fallback.
    A column the export does not have simply maps to "" — it is not an error,
    because only the handful of fields `validate.py` rules on are required.
    """
    folded: dict[str, str] = {}
    for header, value in raw.items():
        key = fold_header(str(header))
        # First header wins, so a duplicated column (Google Forms appends a
        # numeric suffix, which folds away) cannot blank an earlier answer.
        folded.setdefault(key, value)
        folded.setdefault(" ".join(str(header).split()).lower(), value)

    resolved: dict[str, str] = {}
    for column, aliases in COLUMN_ALIASES.items():
        if column in raw:
            resolved[column] = raw[column]
            continue
        resolved[column] = next(
            (folded[alias] for alias in aliases if folded.get(alias)),
            "",
        )
    return resolved


@dataclass
class ParsedRow:
    """One CSV row after parsing, before any rejection rule is applied."""

    row_number: int
    email: str
    full_name: str
    phone: str
    sub_division_1: str
    sub_division_2: str
    division_1: DivisionCode | None
    division_2: DivisionCode | None
    availability_slots: list[str]
    submitted_at: datetime | None
    notes: str | None
    student_id: str = ""


def parse_availability_cell(raw: str) -> list[tuple[Time, Time]]:
    """Parse "18:00-18:30, 19:00 - 19:30" into [(18:00, 18:30), (19:00, 19:30)].

    Retained for exports/sheets that still supply explicit time-block strings;
    the live IFF form uses `parse_preferred_dates` instead.
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    windows: list[tuple[Time, Time]] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        start_str, _, end_str = token.partition("-")
        windows.append((Time.fromisoformat(start_str.strip()), Time.fromisoformat(end_str.strip())))
    return windows


def parse_preferred_dates(raw: str, event: EventConfig) -> dict[Date, list[tuple[Time, Time]]]:
    """Turn a "Preferred Interview Date" cell into per-day availability windows.

    Cells look like "Thursday, 18 September 2025" (a single value, or several
    comma-joined if the form allowed multiple picks). The committee's Applicants
    tab also stores the short form this codebase renders elsewhere —
    "Thu 17 Sep", "Fri 18 Sep", or "Thu 17 Sep; Fri 18 Sep" (see
    `domain/availability.summarise_availability`) — so both the full weekday
    name *and* the configured day label (e.g. "Thu"/"Fri") are matched, each on
    a whole-word boundary so "Thu" does not also fire on "Thursday" and vice
    versa. Every event day named makes that whole day available — the window is
    the day's configured opening hours, so the solver may place the interview
    in any slot that day. Days not named are left out entirely: a "Fri 18 Sep"
    cell yields Friday only, never a fallback to the first/Thursday day. An
    empty or unrecognised cell yields no availability, which `validate.py`
    turns into a NO_AVAILABILITY warning (E-02) rather than a guess.
    """
    text = (raw or "").lower()
    if not text:
        return {}

    def named(token: str) -> bool:
        token = token.strip().lower()
        return bool(token) and re.search(rf"\b{re.escape(token)}\b", text) is not None

    windows: dict[Date, list[tuple[Time, Time]]] = {}
    for day in event.days:
        if named(day.date.strftime("%A")) or named(day.label):
            windows[day.date] = [(day.start, day.end)]
    return windows


def merge_windows(windows: list[tuple[Time, Time]]) -> list[tuple[Time, Time]]:
    """Merge overlapping/touching windows so a slot spanning two adjacent ticked
    blocks is recognised as fully covered (SPEC.md §9.2: "some slots straddle a
    block boundary")."""
    if not windows:
        return []
    ordered = sorted(windows, key=lambda w: w[0])
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _to_seconds(t: Time) -> float:
    return (datetime.combine(Date.min, t) - datetime.combine(Date.min, Time.min)).total_seconds()


def _slot_is_covered(
    slot_start: Time,
    slot_end: Time,
    windows: list[tuple[Time, Time]],
    matching: Literal["strict", "lenient"],
) -> bool:
    if matching == "strict":
        return any(w_start <= slot_start and slot_end <= w_end for w_start, w_end in windows)

    slot_start_s, slot_end_s = _to_seconds(slot_start), _to_seconds(slot_end)
    slot_seconds = slot_end_s - slot_start_s
    total_overlap = 0.0
    for w_start, w_end in windows:
        overlap = min(slot_end_s, _to_seconds(w_end)) - max(slot_start_s, _to_seconds(w_start))
        if overlap > 0:
            total_overlap += overlap
    return total_overlap >= slot_seconds / 2


def blocks_to_slot_ids(
    availability_by_day: dict[Date, list[tuple[Time, Time]]],
    grid: SlotGrid,
    matching: Literal["strict", "lenient"],
) -> list[str]:
    """Resolve an applicant's ticked blocks to the grid slots they cover (E-05)."""
    merged_by_day = {day: merge_windows(windows) for day, windows in availability_by_day.items()}
    covered: list[str] = []
    for slot in grid.slots:
        windows = merged_by_day.get(slot.date, [])
        if _slot_is_covered(slot.start_time, slot.end_time, windows, matching):
            covered.append(slot.slot_id)
    return covered


def map_sub_division(raw: str, mapping: Mapping[str, DivisionCode]) -> DivisionCode | None:
    """Match a sub-division name to its parent division (FR-03).

    Case and whitespace are folded — the form's own option text is not always
    byte-identical to the config's — but nothing is guessed beyond that: an
    unrecognised name returns None and `validate.py` rejects it.

    Accepts either the raw `sub_division_mapping` or the canonical one from
    `DivisionsConfig.canonical_sub_division_mapping`; both are folded here.
    """
    key = canonical_sub_division(raw)
    if not key:
        return None
    folded = {canonical_sub_division(name): code for name, code in mapping.items()}
    return folded.get(key)


# Google Forms writes its own submission timestamp in the sheet's locale, not
# ISO 8601. The live IFF sheet is day-first (12/09/2025 is 12 September), so
# that is tried before the US month-first ordering; an unambiguous value such
# as 8/15/2026 falls through to month-first on its own. ISO is tried first of
# all. Anything unparsable stays None — the timestamp only orders duplicate
# submissions, so validate.py warns rather than rejecting the applicant.
_TIMESTAMP_FORMATS = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%d/%m/%Y",
    "%Y-%m-%d",
)


def parse_timestamp(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def parse_row(
    raw: Mapping[str, str],
    row_number: int,
    event: EventConfig,
    divisions: DivisionsConfig,
    grid: SlotGrid,
) -> ParsedRow:
    cell = resolve_columns(raw)
    mapping = divisions.canonical_sub_division_mapping

    sub_division_1 = cell[COLUMN_SUBDIVISION_1].strip()
    sub_division_2 = cell[COLUMN_SUBDIVISION_2].strip()

    availability_by_day = parse_preferred_dates(cell[COLUMN_PREFERRED_DATE], event)

    return ParsedRow(
        row_number=row_number,
        email=cell[COLUMN_EMAIL].strip().lower(),
        full_name=cell[COLUMN_FULL_NAME].strip(),
        phone=cell[COLUMN_PHONE].strip(),
        student_id=cell[COLUMN_STUDENT_ID].strip(),
        sub_division_1=sub_division_1,
        sub_division_2=sub_division_2,
        division_1=map_sub_division(sub_division_1, mapping),
        division_2=map_sub_division(sub_division_2, mapping),
        availability_slots=blocks_to_slot_ids(
            availability_by_day, grid, event.availability_matching
        ),
        submitted_at=parse_timestamp(cell[COLUMN_TIMESTAMP]),
        notes=cell[COLUMN_NOTES].strip() or None,
    )


def dedupe_by_email(rows: list[ParsedRow]) -> tuple[list[ParsedRow], list[ParsedRow]]:
    """Keep the most recent submission per email, by timestamp then row order
    (FR-05, E-04). Rows with a blank email are never grouped with each other —
    each is left for validate.py to reject on its own merits."""
    groups: dict[str, list[ParsedRow]] = defaultdict(list)
    order: list[str] = []
    for row in rows:
        if row.email not in groups:
            order.append(row.email)
        groups[row.email].append(row)

    kept: list[ParsedRow] = []
    collapsed: list[ParsedRow] = []
    for email in order:
        group = groups[email]
        if not email or len(group) == 1:
            kept.extend(group)
            continue
        winner = max(group, key=lambda r: (r.submitted_at or datetime.min, r.row_number))
        kept.append(winner)
        collapsed.extend(r for r in group if r is not winner)
    return kept, collapsed
