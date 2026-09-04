"""Dedupe, sub-division -> parent mapping, availability parsing (FR-02, FR-03,
FR-05, FR-04b; SPEC.md §12 E-01, E-04, E-05).

Nothing here rejects a row outright — an unmapped sub-division becomes a
`None` division, a bad timestamp becomes a `None` submitted_at, an applicant
with no overlapping availability gets an empty slot list. `validate.py` is
the single place that turns those into rejections, so the reasons stay in
one report (CLAUDE.md invariant 3: "nothing is guessed").

Raw column names follow the Google Form fields in SPEC.md §9.1. Per-day
availability columns are addressed by the day's configured `label`
(`config/event.yaml`), e.g. "Availability — Thu", "Availability — Fri" — this
keeps the mapping driven by config rather than by parsing a formatted date
out of the header text.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime
from datetime import time as Time
from typing import Literal

from iff_scheduler.domain.enums import DivisionCode
from iff_scheduler.domain.grid import SlotGrid
from iff_scheduler.settings import DivisionsConfig, EventConfig

COLUMN_TIMESTAMP = "Timestamp"
COLUMN_EMAIL = "Email address"
COLUMN_FULL_NAME = "Full name"
COLUMN_PHONE = "Phone / WhatsApp"
COLUMN_SUBDIVISION_1 = "First-choice sub-division"
COLUMN_SUBDIVISION_2 = "Second-choice sub-division"
COLUMN_AVAILABILITY_PREFIX = "Availability — "
COLUMN_NOTES = "Accessibility / scheduling notes"


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


def parse_availability_cell(raw: str) -> list[tuple[Time, Time]]:
    """Parse "18:00-18:30, 19:00 - 19:30" into [(18:00, 18:30), (19:00, 19:30)]."""
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
    """Exact-match a sub-division name to its parent division. No fuzzy matching —
    an unrecognised name is a validation failure, not a guess (FR-03)."""
    return mapping.get((raw or "").strip())


def parse_timestamp(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def parse_row(
    raw: Mapping[str, str],
    row_number: int,
    event: EventConfig,
    divisions: DivisionsConfig,
    grid: SlotGrid,
) -> ParsedRow:
    sub_division_1 = (raw.get(COLUMN_SUBDIVISION_1) or "").strip()
    sub_division_2 = (raw.get(COLUMN_SUBDIVISION_2) or "").strip()

    availability_by_day: dict[Date, list[tuple[Time, Time]]] = {}
    for day in event.days:
        column = f"{COLUMN_AVAILABILITY_PREFIX}{day.label}"
        availability_by_day[day.date] = parse_availability_cell(raw.get(column) or "")

    return ParsedRow(
        row_number=row_number,
        email=(raw.get(COLUMN_EMAIL) or "").strip().lower(),
        full_name=(raw.get(COLUMN_FULL_NAME) or "").strip(),
        phone=(raw.get(COLUMN_PHONE) or "").strip(),
        sub_division_1=sub_division_1,
        sub_division_2=sub_division_2,
        division_1=map_sub_division(sub_division_1, divisions.sub_division_mapping),
        division_2=map_sub_division(sub_division_2, divisions.sub_division_mapping),
        availability_slots=blocks_to_slot_ids(
            availability_by_day, grid, event.availability_matching
        ),
        submitted_at=parse_timestamp(raw.get(COLUMN_TIMESTAMP) or ""),
        notes=(raw.get(COLUMN_NOTES) or "").strip() or None,
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
