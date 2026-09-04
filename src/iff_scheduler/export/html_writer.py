"""Printable HTML timetables (FR-53, FR-54).

Rendered with Jinja2 from `templates/timetable/`. This is I/O — reading
template files and writing HTML to disk — so it lives in `export/`, not
`domain/` or `scheduling/` (CLAUDE.md, "Architecture rule").
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from iff_scheduler.export.applicant_view import ApplicantViewRow
from iff_scheduler.export.panel_view import PanelView
from iff_scheduler.export.room_view import RoomView

# templates/ lives at the repo root: src/iff_scheduler/export/html_writer.py -> parents[3]
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates"


def _environment(templates_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "j2"]),
    )


def write_room_view_html(
    room_views: Sequence[RoomView],
    out_dir: Path,
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
) -> list[Path]:
    """One printable page per room per day (FR-50)."""
    env = _environment(templates_dir)
    template = env.get_template("timetable/room_view.html.j2")
    out_dir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for view in room_views:
        html = template.render(
            room_id=view.room_id,
            date=view.date.isoformat(),
            day_label=view.day_label,
            panel_ids=view.panel_ids,
            panel_divisions={pid: div.value for pid, div in view.panel_divisions.items()},
            rows=view.rows,
        )
        path = out_dir / f"room_{view.room_id}_{view.date.isoformat()}.html"
        path.write_text(html, encoding="utf-8")
        written.append(path)
    return written


def write_applicant_view_html(
    rows: Sequence[ApplicantViewRow],
    out_dir: Path,
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
) -> Path:
    """One page listing every applicant's two interviews (FR-51)."""
    env = _environment(templates_dir)
    template = env.get_template("timetable/applicant_view.html.j2")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "applicants.html"
    path.write_text(template.render(rows=rows), encoding="utf-8")
    return path


def write_panel_view_html(
    panel_views: Sequence[PanelView],
    out_dir: Path,
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
) -> Path:
    """One page per panel, separated by print page breaks — their running
    order for the day (FR-52)."""
    env = _environment(templates_dir)
    template = env.get_template("timetable/panel_view.html.j2")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "panels.html"
    path.write_text(template.render(panel_views=panel_views), encoding="utf-8")
    return path
