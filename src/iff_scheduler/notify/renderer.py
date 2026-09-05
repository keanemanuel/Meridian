"""Builds and renders personalised invite emails from a solved schedule
(FR-60, SPEC.md §10.2 steps 1-2).

Split the same way `export/applicant_view.py` and `export/html_writer.py`
split room/applicant views: a pure view-builder (`build_invite_recipients`)
and a templated-I/O layer (`render_invites`, `write_rendered_emails`). Kept
in `notify/`, not `export/`, because its output feeds a `Mailer` rather than
a printable artefact (CLAUDE.md, "Architecture rule").
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date as Date
from datetime import time as Time
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from iff_scheduler.domain.models import Assignment, ChoiceIndex
from iff_scheduler.settings import DivisionsConfig, EventConfig, NotifyConfig

# templates/ lives at the repo root: src/iff_scheduler/notify/renderer.py -> parents[3]
DEFAULT_TEMPLATES_DIR = Path(__file__).resolve().parents[3] / "templates"

INVITE_HTML_TEMPLATE = "email/invite.html.j2"
INVITE_TEXT_TEMPLATE = "email/invite.txt.j2"


@dataclass(frozen=True)
class InviteInterview:
    """One resolved interview, merge-field-ready (SPEC.md §10.3)."""

    choice_index: ChoiceIndex
    division_display: str
    sub_division: str
    day_label: str
    date: Date
    start_time: Time
    end_time: Time
    room: str
    is_clash: bool


@dataclass(frozen=True)
class InviteRecipient:
    """One applicant's invite, before templating. Either interview may be
    missing — that is a C1 violation the audit (FR-64) turns into a hard
    fail rather than a guess (CLAUDE.md invariant 3)."""

    applicant_id: str
    full_name: str
    email: str
    interview_1: InviteInterview | None
    interview_2: InviteInterview | None

    @property
    def is_complete(self) -> bool:
        return self.interview_1 is not None and self.interview_2 is not None

    @property
    def has_clash(self) -> bool:
        """True if either interview falls outside the applicant's stated
        availability (FR-34) — SPEC.md §10.3 holds these back from the
        automated send."""
        return (self.interview_1 is not None and self.interview_1.is_clash) or (
            self.interview_2 is not None and self.interview_2.is_clash
        )


def day_label_map(event: EventConfig) -> dict[Date, str]:
    """date -> configured day label (e.g. "Thu"), for merge field `day_label`."""
    return {day.date: day.label for day in event.days}


def _interview_view(
    assignment: Assignment | None,
    display_by_code: Mapping[str, str],
    day_labels: Mapping[Date, str],
) -> InviteInterview | None:
    if assignment is None:
        return None
    return InviteInterview(
        choice_index=assignment.choice_index,
        division_display=display_by_code.get(assignment.division.value, assignment.division.value),
        sub_division=assignment.sub_division,
        day_label=day_labels.get(assignment.date, ""),
        date=assignment.date,
        start_time=assignment.start_time,
        end_time=assignment.end_time,
        room=assignment.room,
        is_clash=assignment.is_clash,
    )


def build_invite_recipients(
    assignments: Sequence[Assignment],
    divisions: DivisionsConfig,
    event: EventConfig,
) -> list[InviteRecipient]:
    """One row per applicant_id present in `assignments`, sorted for
    determinism (FR-35)."""
    display_by_code = {d.code.value: d.display for d in divisions.divisions}
    day_labels = day_label_map(event)

    by_applicant: dict[str, dict[ChoiceIndex, Assignment]] = defaultdict(dict)
    meta: dict[str, tuple[str, str]] = {}
    for a in assignments:
        by_applicant[a.applicant_id][a.choice_index] = a
        meta[a.applicant_id] = (a.full_name, a.email)

    recipients: list[InviteRecipient] = []
    for applicant_id in sorted(by_applicant):
        choices = by_applicant[applicant_id]
        full_name, email = meta[applicant_id]
        recipients.append(
            InviteRecipient(
                applicant_id=applicant_id,
                full_name=full_name,
                email=email,
                interview_1=_interview_view(choices.get(1), display_by_code, day_labels),
                interview_2=_interview_view(choices.get(2), display_by_code, day_labels),
            )
        )
    return recipients


@dataclass(frozen=True)
class RenderedEmail:
    """One fully-rendered email, ready for a `Mailer`. Carries exactly one
    recipient's data (FR-61)."""

    applicant_id: str
    to_email: str
    to_name: str
    subject: str
    html_body: str
    text_body: str


def _environment(templates_dir: Path) -> Environment:
    """Both invite templates end in `.j2`, so the extension-based
    `select_autoescape` `export/html_writer.py` uses (which keys off the
    *last* dot) would escape the plain-text body too. Key off the full
    `.html.j2` suffix instead so only the HTML template is escaped."""
    return Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=lambda name: name is not None and name.endswith(".html.j2"),
    )


def render_invites(
    recipients: Sequence[InviteRecipient],
    event: EventConfig,
    notify: NotifyConfig,
    templates_dir: Path = DEFAULT_TEMPLATES_DIR,
) -> list[RenderedEmail]:
    """Render every recipient's invite to HTML + plain text (SPEC.md §10.2
    step 2, FR-60)."""
    env = _environment(templates_dir)
    html_template = env.get_template(INVITE_HTML_TEMPLATE)
    text_template = env.get_template(INVITE_TEXT_TEMPLATE)

    context_base = {
        "event_name": event.event_name,
        "sender_name": notify.sender_name,
        "reply_to": notify.reply_to,
        "contact_name": notify.contact_name,
        "contact_channel": notify.contact_channel,
        "rsvp_deadline": notify.rsvp_deadline,
        "arrival_minutes_early": notify.arrival_minutes_early,
        "what_to_bring": notify.what_to_bring,
    }

    rendered: list[RenderedEmail] = []
    for recipient in recipients:
        context = {
            **context_base,
            "full_name": recipient.full_name,
            "interview_1": recipient.interview_1,
            "interview_2": recipient.interview_2,
        }
        rendered.append(
            RenderedEmail(
                applicant_id=recipient.applicant_id,
                to_email=recipient.email,
                to_name=recipient.full_name,
                subject=f"{event.event_name} — Your Interview Schedule",
                html_body=html_template.render(**context),
                text_body=text_template.render(**context),
            )
        )
    return rendered


def write_rendered_emails(rendered: Sequence[RenderedEmail], out_dir: Path) -> list[Path]:
    """Dry-run output: one `.html` and one `.txt` file per applicant (FR-63)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for email in rendered:
        html_path = out_dir / f"{email.applicant_id}.html"
        text_path = out_dir / f"{email.applicant_id}.txt"
        html_path.write_text(email.html_body, encoding="utf-8")
        text_path.write_text(email.text_body, encoding="utf-8")
        written.extend([html_path, text_path])
    return written
