"""Invite and result emails (FR-60..FR-67, SPEC.md §10).

`/preview` is the CLI's `--dry-run`: render every email, run the pre-send
audit, write the files, return counts + samples. `/send` is the CLI's
`--send`: it additionally requires a typed recipient count (`confirm_count`,
FR-64), checks the ledger for idempotency (FR-62), and — for results — a
`verified_by` name (SPEC.md §10.4). Clash invites are held back for manual
sending (SPEC.md §10.3).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.cli_helpers import (
    load_assignments,
    load_clean_applicants,
    load_ledger,
    send_batch,
)
from api.dependencies import get_settings, resolve_run_dir
from iff_scheduler import workspace as ws
from iff_scheduler.domain.enums import Decision, DivisionCode
from iff_scheduler.notify.audit import (
    audit_invite_recipients,
    audit_result_recipients,
    partition_clash_recipients,
)
from iff_scheduler.notify.gmail_mailer import GmailMailer
from iff_scheduler.notify.ledger import already_sent
from iff_scheduler.notify.renderer import (
    build_invite_recipients,
    render_invites,
    render_results,
    write_rendered_emails,
)
from iff_scheduler.results.decide import build_result_recipients, partition_by_decision
from iff_scheduler.results.ingest_scores import read_raw_scores, run_ingest_scores
from iff_scheduler.settings import Settings

router = APIRouter(prefix="/api/workspaces/{workspace_id}/runs/{run_id}/notify", tags=["notify"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
INVITE_TEMPLATE = "invite"


def _samples(rendered: list[Any], limit: int = 3) -> list[dict[str, str]]:
    return [
        {
            "applicant_id": e.applicant_id,
            "to_email": e.to_email,
            "subject": e.subject,
            "text_body": e.text_body,
        }
        for e in rendered[:limit]
    ]


def _load_invite_inputs(workspace_id: str, run_id: str, settings: Settings):
    run_dir = resolve_run_dir(workspace_id, run_id)
    assignments_path = run_dir / "assignments.csv"
    if not assignments_path.exists():
        raise HTTPException(status_code=404, detail=f"{assignments_path} not found — solve first.")
    assignments = load_assignments(assignments_path)
    recipients = build_invite_recipients(assignments, settings.divisions, settings.event)
    issues = audit_invite_recipients(recipients)
    if issues:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"{len(issues)} audit issue(s) — nothing rendered or sent (FR-64).",
                "issues": [
                    {"applicant_id": i.applicant_id, "code": i.code, "message": i.message}
                    for i in issues
                ],
            },
        )
    return run_dir, recipients


def _gmail_mailer(settings: Settings) -> GmailMailer:
    load_dotenv()
    oauth_file = os.environ.get("GMAIL_OAUTH_CREDENTIALS")
    token_cache_file = os.environ.get("GMAIL_TOKEN_CACHE")
    sender_email = settings.notify.sender_email or os.environ.get("GMAIL_SENDER_EMAIL", "")
    missing = [
        name
        for name, val in [
            ("GMAIL_OAUTH_CREDENTIALS", oauth_file),
            ("GMAIL_TOKEN_CACHE", token_cache_file),
            ("sender_email/GMAIL_SENDER_EMAIL", sender_email),
        ]
        if not val
    ]
    if missing:
        raise HTTPException(
            status_code=409, detail=f"Email sending not configured: missing {', '.join(missing)}."
        )
    return GmailMailer(
        oauth_credentials_file=oauth_file,
        token_cache_file=token_cache_file,
        sender_email=sender_email,
        sender_name=settings.notify.sender_name,
    )


def _run_send(mailer: GmailMailer, pending, rendered_by_applicant, ledger, ledger_path, run_id):
    """The CLI's exact send loop (ledger-checked, records every attempt
    immediately, keeps going past a failure — SPEC.md §10.2 steps 5-6)."""
    return send_batch(
        mailer=mailer,
        pending=pending,
        rendered_by_applicant=rendered_by_applicant,
        ledger=ledger,
        ledger_path=ledger_path,
        run_id=run_id,
        throttle_seconds=0.0,
        retry_hint="POST .../notify/.../send",
    )


# --------------------------------------------------------------- invite


@router.post("/invite/preview")
def invite_preview(workspace_id: str, run_id: str, settings: SettingsDep) -> dict[str, Any]:
    run_dir, recipients = _load_invite_inputs(workspace_id, run_id, settings)
    auto, manual = partition_clash_recipients(recipients)
    rendered_auto = render_invites(auto, settings.event, settings.notify)
    rendered_manual = render_invites(manual, settings.event, settings.notify)
    emails_dir = run_dir / "emails"
    write_rendered_emails(rendered_auto, emails_dir)
    if manual:
        write_rendered_emails(rendered_manual, emails_dir / "manual_review")
    return {
        "total": len(recipients),
        "auto_sendable": len(auto),
        "held_for_manual": len(manual),
        "emails_dir": str(emails_dir),
        "samples": _samples(rendered_auto),
    }


class SendBody(BaseModel):
    confirm_count: int


@router.post("/invite/send")
def invite_send(
    workspace_id: str, run_id: str, settings: SettingsDep, body: SendBody
) -> dict[str, Any]:
    run_dir, recipients = _load_invite_inputs(workspace_id, run_id, settings)
    auto, _manual = partition_clash_recipients(recipients)
    rendered_auto = render_invites(auto, settings.event, settings.notify)

    ledger_path = ws.send_ledger_path(workspace_id)
    ledger = load_ledger(ledger_path)
    pending = [r for r in auto if not already_sent(ledger, r.applicant_id, INVITE_TEMPLATE)]
    if not pending:
        return {"sent": 0, "pending": 0, "message": "All auto-sendable invites already SENT."}
    if body.confirm_count != len(pending):
        raise HTTPException(
            status_code=400,
            detail=f"confirm_count {body.confirm_count} != {len(pending)} pending. Nothing sent.",
        )

    mailer = _gmail_mailer(settings)
    rendered_by_applicant = {e.applicant_id: e for e in rendered_auto}
    _run_send(
        mailer,
        [(r.applicant_id, r.email, INVITE_TEMPLATE) for r in pending],
        rendered_by_applicant,
        ledger,
        ledger_path,
        run_dir.resolve().name,
    )
    final = load_ledger(ledger_path)
    sent = sum(1 for e in final if e.template == INVITE_TEMPLATE and e.status.value == "SENT")
    failed = sum(1 for e in final if e.template == INVITE_TEMPLATE and e.status.value == "FAILED")
    return {"sent_total": sent, "failed_total": failed, "attempted": len(pending)}


# --------------------------------------------------------------- result


def _load_result_inputs(workspace_id: str, run_id: str, settings: Settings):
    run_dir = resolve_run_dir(workspace_id, run_id)
    applicants_path = ws.applicants_clean_path(workspace_id)
    scores_path = ws.scores_path(workspace_id)
    if not applicants_path.exists():
        raise HTTPException(status_code=404, detail=f"No applicants at {applicants_path}.")
    if not scores_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No scores at {scores_path}. Export the committee's scoring sheet there.",
        )
    applicants = load_clean_applicants(applicants_path)
    known = {code.value for code in DivisionCode}
    score_result = run_ingest_scores(read_raw_scores(scores_path), known)
    rejected = [r for r in score_result.report if r.outcome == "REJECTED"]
    if rejected:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"{len(rejected)} score row(s) rejected — nothing rendered or sent.",
                "issues": [
                    {"applicant_id": r.applicant_id, "code": r.reason_code, "message": r.message}
                    for r in rejected
                ],
            },
        )
    recipients, decision_issues = build_result_recipients(
        applicants, score_result.records, settings.divisions
    )
    if decision_issues:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"{len(decision_issues)} decision issue(s) — nothing sent.",
                "issues": [
                    {"applicant_id": i.applicant_id, "code": i.code, "message": i.message}
                    for i in decision_issues
                ],
            },
        )
    audit_issues = audit_result_recipients(recipients, settings.notify)
    if audit_issues:
        raise HTTPException(
            status_code=400,
            detail={
                "message": f"{len(audit_issues)} audit issue(s) — nothing sent.",
                "issues": [
                    {"applicant_id": i.applicant_id, "code": i.code, "message": i.message}
                    for i in audit_issues
                ],
            },
        )
    return run_dir, recipients


@router.post("/result/preview")
def result_preview(workspace_id: str, run_id: str, settings: SettingsDep) -> dict[str, Any]:
    run_dir, recipients = _load_result_inputs(workspace_id, run_id, settings)
    rendered = render_results(recipients, settings.event, settings.notify)
    emails_dir = run_dir / "results_emails"
    write_rendered_emails(rendered, emails_dir)
    grouped = partition_by_decision(recipients)
    return {
        "counts": {d.value: len(grouped[d]) for d in Decision},
        "emails_dir": str(emails_dir),
        "samples": _samples(rendered),
    }


class ResultSendBody(BaseModel):
    confirm_count: int
    verified_by: str


@router.post("/result/send")
def result_send(
    workspace_id: str, run_id: str, settings: SettingsDep, body: ResultSendBody
) -> dict[str, Any]:

    if not body.verified_by.strip():
        raise HTTPException(
            status_code=400, detail="verified_by is required for a result send (SPEC.md §10.4)."
        )
    run_dir, recipients = _load_result_inputs(workspace_id, run_id, settings)
    rendered = render_results(recipients, settings.event, settings.notify)

    ledger_path = ws.send_ledger_path(workspace_id)
    ledger = load_ledger(ledger_path)
    pending = [r for r in recipients if not already_sent(ledger, r.applicant_id, r.template_name)]
    if not pending:
        return {"sent": 0, "pending": 0, "message": "All result emails already SENT."}
    if body.confirm_count != len(pending):
        raise HTTPException(
            status_code=400,
            detail=f"confirm_count {body.confirm_count} != {len(pending)} pending. Nothing sent.",
        )

    mailer = _gmail_mailer(settings)
    (run_dir / "results_emails").mkdir(parents=True, exist_ok=True)
    (run_dir / "results_emails" / "verification_log.csv").open("a", encoding="utf-8").write(
        f"{datetime.now().isoformat()},{run_dir.resolve().name},{body.verified_by.strip()},"
        f"{len(pending)}\n"
    )
    rendered_by_applicant = {e.applicant_id: e for e in rendered}
    _run_send(
        mailer,
        [(r.applicant_id, r.email, r.template_name) for r in pending],
        rendered_by_applicant,
        ledger,
        ledger_path,
        run_dir.resolve().name,
    )
    final = load_ledger(ledger_path)
    sent = sum(1 for e in final if e.status.value == "SENT")
    return {"sent_total": sent, "attempted": len(pending), "verified_by": body.verified_by.strip()}
