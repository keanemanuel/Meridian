"""Pre-send audit (FR-64, SPEC.md §10.2 step 3).

Hard-fails a send before any email leaves the building: any merge field
that renders blank/"None"/"nan", any invalid or duplicate address, or any
applicant missing an assignment. Pure — works on already-built
`InviteRecipient` rows, no I/O, no network.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from iff_scheduler.domain.enums import Decision
from iff_scheduler.notify.renderer import InviteRecipient
from iff_scheduler.results.decide import ResultRecipient
from iff_scheduler.settings import NotifyConfig

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_BLANK_VALUES = frozenset({"", "none", "nan", "null"})


@dataclass(frozen=True)
class AuditIssue:
    """One reason a batch cannot go out yet. `applicant_id` is blank only
    for a batch-wide issue, never for a per-recipient one."""

    applicant_id: str
    code: str
    message: str


def _is_blank(value: str | None) -> bool:
    return value is None or value.strip().lower() in _BLANK_VALUES


def audit_invite_recipients(recipients: Sequence[InviteRecipient]) -> list[AuditIssue]:
    """Every reason this batch cannot be rendered/sent yet. Empty means the
    batch is clear (FR-64). Never raises — the caller decides what "hard
    fail" looks like for its context (mirrors `review.edit_validator`)."""
    issues: list[AuditIssue] = []
    seen_emails: dict[str, str] = {}

    for r in recipients:
        if not r.is_complete:
            issues.append(
                AuditIssue(
                    r.applicant_id,
                    "MISSING_ASSIGNMENT",
                    f"{r.applicant_id} ({r.full_name}) is missing one or both interview "
                    "assignments (C1) — cannot invite until the schedule is complete.",
                )
            )

        if _is_blank(r.email) or not _EMAIL_RE.match(r.email):
            issues.append(
                AuditIssue(
                    r.applicant_id,
                    "INVALID_EMAIL",
                    f"{r.applicant_id} has an invalid email address: {r.email!r}.",
                )
            )
        else:
            key = r.email.strip().lower()
            duplicate_of = seen_emails.get(key)
            if duplicate_of is not None:
                issues.append(
                    AuditIssue(
                        r.applicant_id,
                        "DUPLICATE_EMAIL",
                        f"{r.applicant_id} shares email address {r.email!r} with applicant "
                        f"{duplicate_of} (FR-61) — refusing to send until this is resolved.",
                    )
                )
            else:
                seen_emails[key] = r.applicant_id

        if _is_blank(r.full_name):
            issues.append(
                AuditIssue(r.applicant_id, "BLANK_FIELD", f"{r.applicant_id} has a blank name.")
            )

        for interview in (r.interview_1, r.interview_2):
            if interview is None:
                continue
            for field_name, value in (
                ("division_display", interview.division_display),
                ("sub_division", interview.sub_division),
                ("day_label", interview.day_label),
                ("room", interview.room),
            ):
                if _is_blank(value):
                    issues.append(
                        AuditIssue(
                            r.applicant_id,
                            "BLANK_FIELD",
                            f"{r.applicant_id} choice {interview.choice_index}: "
                            f"'{field_name}' rendered blank.",
                        )
                    )

    return issues


def audit_result_recipients(
    recipients: Sequence[ResultRecipient], notify: NotifyConfig
) -> list[AuditIssue]:
    """Every reason a results batch cannot be rendered/sent yet (SPEC.md
    §10.4, FR-65). Run twice, per SPEC.md §10.4 — "a merge-field error here
    means telling someone the wrong outcome" — by the CLI calling this
    before *and* after the second-person verification gate. Never raises,
    same contract as `audit_invite_recipients`."""
    issues: list[AuditIssue] = []

    for field_name, value in (
        ("next_steps_accepted", notify.next_steps_accepted),
        ("next_steps_waitlist", notify.next_steps_waitlist),
        ("next_steps_rejected", notify.next_steps_rejected),
    ):
        if _is_blank(value):
            issues.append(
                AuditIssue(
                    "",
                    "BLANK_CONFIG_FIELD",
                    f"config/notify.yaml: '{field_name}' is blank — every result email must "
                    "give a real next step (SPEC.md §10.4).",
                )
            )

    seen_emails: dict[str, str] = {}
    for r in recipients:
        if _is_blank(r.email) or not _EMAIL_RE.match(r.email):
            issues.append(
                AuditIssue(
                    r.applicant_id,
                    "INVALID_EMAIL",
                    f"{r.applicant_id} has an invalid email address: {r.email!r}.",
                )
            )
        else:
            key = r.email.strip().lower()
            duplicate_of = seen_emails.get(key)
            if duplicate_of is not None:
                issues.append(
                    AuditIssue(
                        r.applicant_id,
                        "DUPLICATE_EMAIL",
                        f"{r.applicant_id} shares email address {r.email!r} with applicant "
                        f"{duplicate_of} (FR-61) — refusing to send until this is resolved.",
                    )
                )
            else:
                seen_emails[key] = r.applicant_id

        if _is_blank(r.full_name):
            issues.append(
                AuditIssue(r.applicant_id, "BLANK_FIELD", f"{r.applicant_id} has a blank name.")
            )

        if r.decision == Decision.ACCEPTED and _is_blank(r.division_placed_display):
            issues.append(
                AuditIssue(
                    r.applicant_id,
                    "BLANK_FIELD",
                    f"{r.applicant_id} is ACCEPTED but 'division_placed' rendered blank.",
                )
            )

    return issues


def partition_clash_recipients(
    recipients: Sequence[InviteRecipient],
) -> tuple[list[InviteRecipient], list[InviteRecipient]]:
    """Split into (auto-sendable, held-for-manual-send).

    A clash assignment sits outside the applicant's stated availability
    (FR-34); SPEC.md §10.3 requires a human to send that one personally,
    with an apology — never the machine, silently. Order is preserved
    (recipients already arrive sorted, FR-35)."""
    auto = [r for r in recipients if not r.has_clash]
    manual = [r for r in recipients if r.has_clash]
    return auto, manual
