"""Joins scores to applicants and produces the decision column (SPEC.md
§10.4, §13 M7). The completeness rule that matters most lives here: every
applicant in the roster must have exactly one decision, and a missing one is
reported as `MISSING_DECISION` rather than silently treated as REJECTED
(CLAUDE.md invariant 3).

Pure — like `review.edit_validator`, this returns a list of issues instead of
raising, so the CLI decides what "hard failure" looks like.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from iff_scheduler.domain.enums import Decision
from iff_scheduler.domain.models import Applicant, DecisionRecord
from iff_scheduler.settings import DivisionsConfig

TEMPLATE_BY_DECISION: dict[Decision, str] = {
    Decision.ACCEPTED: "result_accepted",
    Decision.WAITLIST: "result_waitlist",
    Decision.REJECTED: "result_rejected",
}


@dataclass(frozen=True)
class ResultRecipient:
    """One applicant's result, before templating. `division_placed_display`
    is set only for ACCEPTED (SPEC.md §10.4)."""

    applicant_id: str
    full_name: str
    email: str
    decision: Decision
    division_placed_display: str | None
    template_name: str


@dataclass(frozen=True)
class DecisionIssue:
    """One reason the batch cannot be routed yet. Mirrors
    `review.edit_validator.EditViolation` and `notify.audit.AuditIssue`."""

    applicant_id: str
    code: str
    message: str


def build_result_recipients(
    applicants: Sequence[Applicant],
    decisions: Sequence[DecisionRecord],
    divisions: DivisionsConfig,
) -> tuple[list[ResultRecipient], list[DecisionIssue]]:
    """Join every applicant in the roster to their decision. Returns
    (recipients, issues) — issues is non-empty if any applicant has no
    decision, more than one, or a decision references an unknown applicant.
    Recipients are only built for applicants that resolve cleanly (FR-35:
    sorted by applicant_id for determinism)."""
    display_by_code = {d.code.value: d.display for d in divisions.divisions}
    applicants_by_id = {a.applicant_id: a for a in applicants}

    decisions_by_id: dict[str, list[DecisionRecord]] = defaultdict(list)
    for record in decisions:
        decisions_by_id[record.applicant_id].append(record)

    issues: list[DecisionIssue] = []
    recipients: list[ResultRecipient] = []

    for applicant_id in sorted(applicants_by_id):
        applicant = applicants_by_id[applicant_id]
        matches = decisions_by_id.get(applicant_id, [])

        if not matches:
            issues.append(
                DecisionIssue(
                    applicant_id,
                    "MISSING_DECISION",
                    f"{applicant_id} ({applicant.full_name}) has no decision recorded — a "
                    "blank decision is a hard failure, never defaulted to rejected "
                    "(SPEC.md §10.4).",
                )
            )
            continue

        if len(matches) > 1:
            issues.append(
                DecisionIssue(
                    applicant_id,
                    "DUPLICATE_DECISION",
                    f"{applicant_id} has {len(matches)} decision records — ambiguous, resolve "
                    "in the scores sheet before re-running.",
                )
            )
            continue

        record = matches[0]
        division_placed_display: str | None = None
        if record.decision == Decision.ACCEPTED:
            if record.division_placed is None:
                issues.append(
                    DecisionIssue(
                        applicant_id,
                        "MISSING_DIVISION_PLACED",
                        f"{applicant_id} is ACCEPTED but has no division_placed.",
                    )
                )
                continue
            division_placed_display = display_by_code.get(
                record.division_placed.value, record.division_placed.value
            )

        recipients.append(
            ResultRecipient(
                applicant_id=applicant_id,
                full_name=applicant.full_name,
                email=applicant.email,
                decision=record.decision,
                division_placed_display=division_placed_display,
                template_name=TEMPLATE_BY_DECISION[record.decision],
            )
        )

    unknown_ids = sorted(set(decisions_by_id) - set(applicants_by_id))
    for unknown_id in unknown_ids:
        issues.append(
            DecisionIssue(
                unknown_id,
                "UNKNOWN_APPLICANT",
                f"Decision recorded for '{unknown_id}', which is not in the applicant roster.",
            )
        )

    return recipients, issues


def partition_by_decision(
    recipients: Sequence[ResultRecipient],
) -> dict[Decision, list[ResultRecipient]]:
    """Group recipients by decision — the shape the accepted/waitlist/rejected
    review lists and the verification gate need (SPEC.md §10.4: "Have a
    second person eyeball the accepted list and the rejected list
    separately")."""
    grouped: dict[Decision, list[ResultRecipient]] = {d: [] for d in Decision}
    for recipient in recipients:
        grouped[recipient.decision].append(recipient)
    return grouped
