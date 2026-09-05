"""Gmail API mailer (SPEC.md §10.1, "our chosen architecture").

Sends one message per call under OAuth2 (installed-app flow), authorised
against the operator's own Gmail account rather than a service account —
so invite and result emails come from a real, recognisable Gmail address.
This is the only place in `notify/` that touches Google credentials or the
network — the rest of the pipeline (renderer, ledger, audit) never imports
this module directly, only the `Mailer` protocol it implements (CLAUDE.md,
"Architecture rule").
"""

from __future__ import annotations

import base64
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from iff_scheduler.notify.base import EmailMessage, SendError

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


class GmailMailer:
    """Sends via the Gmail API under OAuth2, one recipient per call (FR-61).

    On first use, `authorise()` opens a browser for a one-time Gmail
    consent screen and caches the resulting token at `token_cache_file`.
    Every subsequent run reuses that cached token silently; an expired
    token is refreshed automatically without reopening the browser.
    """

    def __init__(
        self,
        oauth_credentials_file: str,
        token_cache_file: str,
        sender_email: str,
        sender_name: str = "",
    ) -> None:
        self._sender_email = sender_email
        self._sender_name = sender_name
        credentials = authorise(oauth_credentials_file, token_cache_file)
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


def authorise(oauth_credentials_file: str, token_cache_file: str) -> Credentials:
    """Return a valid OAuth2 credential, reusing or refreshing the cached
    token at `token_cache_file` where possible and only falling back to the
    interactive browser consent flow when there is no usable token.

    Split out from `GmailMailer.__init__` so tests can exercise the
    cache/refresh/first-run branches without a real browser or network call.
    """
    token_path = Path(token_cache_file)
    credentials: Credentials | None = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)  # type: ignore[no-untyped-call]

    if credentials and credentials.valid:
        return credentials

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())  # type: ignore[no-untyped-call]
    else:
        flow = InstalledAppFlow.from_client_secrets_file(oauth_credentials_file, SCOPES)
        credentials = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(credentials.to_json())
    return credentials
