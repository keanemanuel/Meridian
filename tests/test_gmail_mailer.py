"""Tests for the OAuth2 Gmail mailer (notify/gmail_mailer.py).

`GmailMailer` is the one place in `notify/` that touches Google credentials
and the network (CLAUDE.md, "Architecture rule"), so these tests mock every
Google library call — no real browser, network, or credentials file is ever
used. They cover the three branches `authorise()` can take:

1. A valid cached token is reused silently (no browser, no refresh).
2. An expired-but-refreshable cached token is refreshed automatically and
   the refreshed token is re-cached.
3. No usable cached token triggers the one-time interactive installed-app
   flow, and the resulting token is cached for next time.

And separately, that `GmailMailer.send()` builds and sends a single-message
MIME payload without ever touching the auth machinery.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

from iff_scheduler.notify.base import EmailMessage
from iff_scheduler.notify.gmail_mailer import GmailMailer, authorise


def _fake_credentials(*, valid: bool, expired: bool = False, refresh_token: str | None = None):
    creds = MagicMock()
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    creds.to_json.return_value = '{"token": "cached"}'
    return creds


def test_authorise_reuses_valid_cached_token(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text('{"token": "existing"}')
    cached = _fake_credentials(valid=True)

    with (
        patch("iff_scheduler.notify.gmail_mailer.Credentials") as mock_credentials_cls,
        patch("iff_scheduler.notify.gmail_mailer.InstalledAppFlow") as mock_flow_cls,
    ):
        mock_credentials_cls.from_authorized_user_file.return_value = cached

        result = authorise("unused_client_secrets.json", str(token_path))

    assert result is cached
    mock_flow_cls.from_client_secrets_file.assert_not_called()
    cached.refresh.assert_not_called()


def test_authorise_refreshes_expired_token_without_browser(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    token_path.write_text('{"token": "stale"}')
    stale = _fake_credentials(valid=False, expired=True, refresh_token="refresh-me")

    with (
        patch("iff_scheduler.notify.gmail_mailer.Credentials") as mock_credentials_cls,
        patch("iff_scheduler.notify.gmail_mailer.InstalledAppFlow") as mock_flow_cls,
        patch("iff_scheduler.notify.gmail_mailer.Request") as mock_request_cls,
    ):
        mock_credentials_cls.from_authorized_user_file.return_value = stale

        result = authorise("unused_client_secrets.json", str(token_path))

    stale.refresh.assert_called_once_with(mock_request_cls.return_value)
    mock_flow_cls.from_client_secrets_file.assert_not_called()
    assert result is stale
    assert token_path.read_text() == '{"token": "cached"}'


def test_authorise_runs_installed_app_flow_when_no_cached_token(tmp_path: Path) -> None:
    token_path = tmp_path / "nested" / "token.json"
    assert not token_path.exists()
    fresh = _fake_credentials(valid=True)

    with (
        patch("iff_scheduler.notify.gmail_mailer.InstalledAppFlow") as mock_flow_cls,
    ):
        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = fresh
        mock_flow_cls.from_client_secrets_file.return_value = mock_flow

        result = authorise("client_secrets.json", str(token_path))

    mock_flow_cls.from_client_secrets_file.assert_called_once()
    args, _ = mock_flow_cls.from_client_secrets_file.call_args
    assert args[0] == "client_secrets.json"
    mock_flow.run_local_server.assert_called_once()
    assert result is fresh
    assert token_path.exists()
    assert token_path.read_text() == '{"token": "cached"}'


def test_send_builds_single_recipient_mime_message(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    valid = _fake_credentials(valid=True)

    with (
        patch("iff_scheduler.notify.gmail_mailer.Credentials") as mock_credentials_cls,
        patch("iff_scheduler.notify.gmail_mailer.build") as mock_build,
    ):
        mock_credentials_cls.from_authorized_user_file.return_value = valid
        token_path.write_text('{"token": "existing"}')

        mock_service = MagicMock()
        mock_build.return_value = mock_service
        mock_send = mock_service.users.return_value.messages.return_value.send
        mock_send.return_value.execute.return_value = {"id": "msg-123"}

        mailer = GmailMailer(
            oauth_credentials_file="unused_client_secrets.json",
            token_cache_file=str(token_path),
            sender_email="recruiter@iff.org",
            sender_name="IFF Recruitment",
        )

        message_id = mailer.send(
            EmailMessage(
                to_email="applicant@example.com",
                to_name="Applicant Name",
                subject="Your interview slot",
                html_body="<p>See you there</p>",
                text_body="See you there",
            )
        )

    assert message_id == "msg-123"
    mock_build.assert_called_once_with("gmail", "v1", credentials=valid, cache_discovery=False)

    send_kwargs = mock_service.users.return_value.messages.return_value.send.call_args.kwargs
    assert send_kwargs["userId"] == "me"
    raw = send_kwargs["body"]["raw"]

    decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8", errors="ignore")
    assert "To: applicant@example.com" in decoded
    assert "From: IFF Recruitment <recruiter@iff.org>" in decoded
    assert "Subject: Your interview slot" in decoded
