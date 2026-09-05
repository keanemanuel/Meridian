"""Gmail API mailer (SPEC.md §10.1, "our chosen architecture").

Sends one message per call via a domain-wide-delegated service account
(`credentials/service_account.json`, `GOOGLE_SERVICE_ACCOUNT_FILE`). This is
the only place in `notify/` that touches Google credentials or the network —
the rest of the pipeline (renderer, ledger, audit) never imports this module
directly, only the `Mailer` protocol it implements (CLAUDE.md, "Architecture
rule").
"""

from __future__ import annotations

import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from iff_scheduler.notify.base import EmailMessage, SendError

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class GmailMailer:
    """Sends via the Gmail API, one recipient per call (FR-61)."""

    def __init__(
        self,
        service_account_file: str,
        sender_email: str,
        sender_name: str = "",
    ) -> None:
        self._sender_email = sender_email
        self._sender_name = sender_name
        # google-auth ships no type annotations on this constructor (no py.typed
        # marker) — the only untyped call in this adapter boundary.
        credentials = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
            service_account_file, scopes=SCOPES
        ).with_subject(sender_email)
        self._service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def send(self, message: EmailMessage) -> str:
        mime = MIMEMultipart("alternative")
        mime["To"] = message.to_email
        mime["Subject"] = message.subject
        mime["From"] = (
            f"{self._sender_name} <{self._sender_email}>"
            if self._sender_name
            else self._sender_email
        )
        # Plain text first, HTML last — clients that render multipart/alternative
        # show the last part they understand, so HTML wins where supported.
        mime.attach(MIMEText(message.text_body, "plain"))
        mime.attach(MIMEText(message.html_body, "html"))

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")
        try:
            sent = self._service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except HttpError as exc:
            raise SendError(f"Gmail API send to {message.to_email} failed: {exc}") from exc
        return str(sent["id"])
