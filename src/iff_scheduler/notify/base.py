"""Mailer protocol — the seam between notify and its sending providers
(SPEC.md §10.1).

Swapping the Gmail API sender for a transactional provider means providing a
new class with this shape; nothing upstream (`renderer`, `ledger`, `audit`,
or the CLI's send loop) needs to change. Pure typing module: no network, no
credentials (CLAUDE.md, "Architecture rule").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmailMessage:
    """One outbound email, already rendered for exactly one recipient.

    There is no field for a second address anywhere on this type — FR-61
    ("no applicant shall ever see another applicant's address") is enforced
    by the shape of the data, not by a runtime check.
    """

    to_email: str
    to_name: str
    subject: str
    html_body: str
    text_body: str


class SendError(Exception):
    """Raised by a `Mailer` when one send fails.

    The caller (the CLI's send loop) catches this per-recipient, records a
    FAILED ledger row, and keeps going (FR-66) — one bad address must never
    abort the whole batch.
    """


class Mailer(Protocol):
    def send(self, message: EmailMessage) -> str:
        """Send one message and return the provider's message id.

        Raises `SendError` on failure. Implementations must send to exactly
        the one address on `message` — never CC, BCC, or batch multiple
        recipients into a single call (FR-61, SPEC.md §10.1).
        """
        ...
