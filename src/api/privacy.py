"""Name disguise for a publicly reachable deployment.

Masking is a *per-workspace* policy, not a deployment-wide switch: only the
workspaces named in `MASKED_WORKSPACE_NAMES` (default: "IFF 2026-27
Registration") disguise names at all. Every other workspace — including any
new one created later — always shows real names, with no password gate in
front of it.

For a masked workspace, when `REVEAL_PASSWORD` is set, every response that
would carry an applicant's identity is disguised *on the server* —
`full_name` becomes a stable `CAND0001` style alias and `email` is blanked
out — unless the request carries a valid reveal token. The browser gets that
token by posting the password to `POST /api/workspaces/{workspace_id}/reveal`;
the password itself lives only in the environment (never in git — CLAUDE.md
invariant 7).

Masking here rather than in the frontend is the whole point: a disguise applied
in the browser would still ship the real names in the JSON anyone can read from
the network tab.

When `REVEAL_PASSWORD` is unset (a laptop, the CLI, the test suite), or the
workspace isn't one of the masked ones, nothing is disguised and the API
behaves exactly as before.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from typing import Annotated, Any

from dotenv import load_dotenv
from fastapi import Depends, Header

from iff_scheduler.domain.models import Assignment

REVEAL_PASSWORD_ENV = "REVEAL_PASSWORD"
MASKED_WORKSPACES_ENV = "MASKED_WORKSPACE_NAMES"

# The one workspace this disguise protects by default. Override with a
# comma-separated MASKED_WORKSPACE_NAMES (e.g. in tests, which use a fresh
# per-test workspace name and so can't rely on this fixed one).
DEFAULT_MASKED_WORKSPACE = "IFF 2026-27 Registration"

# How long one successful password entry keeps real names visible.
TOKEN_TTL_SECONDS = 12 * 60 * 60

HIDDEN_EMAIL = "hidden"


def reveal_password() -> str | None:
    load_dotenv()
    return os.environ.get(REVEAL_PASSWORD_ENV) or None


def masked_workspace_names() -> frozenset[str]:
    """Workspace names that disguise applicant identity. Any workspace not
    in this set always shows real names — a new workspace never inherits
    the protection one specific recruitment round needs."""
    load_dotenv()
    raw = os.environ.get(MASKED_WORKSPACES_ENV)
    if raw is None:
        return frozenset({DEFAULT_MASKED_WORKSPACE})
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def masking_enabled(workspace_id: str) -> bool:
    """True when this workspace disguises names for unverified viewers."""
    return reveal_password() is not None and workspace_id in masked_workspace_names()


# ----------------------------------------------------------------- tokens
#
# Stateless on purpose: the API may run as several instances (or cold-start
# between requests), so a token is `<expiry>.<HMAC(expiry)>` keyed off the
# password rather than a server-side session. Changing the password therefore
# also invalidates every token already handed out.


def _signature(password: str, expires_at: int) -> str:
    key = hashlib.sha256(password.encode("utf-8")).digest()
    return hmac.new(key, f"reveal:{expires_at}".encode(), hashlib.sha256).hexdigest()


def password_matches(candidate: str) -> bool:
    password = reveal_password()
    if password is None:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), password.encode("utf-8"))


def issue_token(now: float | None = None) -> tuple[str, int]:
    """A fresh reveal token and the epoch second it expires at."""
    password = reveal_password()
    if password is None:
        raise RuntimeError(f"{REVEAL_PASSWORD_ENV} is not set — nothing to reveal.")
    expires_at = int(now if now is not None else time.time()) + TOKEN_TTL_SECONDS
    return f"{expires_at}.{_signature(password, expires_at)}", expires_at


def token_valid(token: str | None, now: float | None = None) -> bool:
    password = reveal_password()
    if password is None or not token:
        return False
    expires_text, _, signature = token.partition(".")
    if not expires_text.isdigit() or not signature:
        return False
    expires_at = int(expires_text)
    if expires_at <= (now if now is not None else time.time()):
        return False
    return hmac.compare_digest(signature, _signature(password, expires_at))


def names_revealed(
    workspace_id: str, x_reveal_token: Annotated[str | None, Header()] = None
) -> bool:
    """Whether this request may see real names: always when this workspace
    isn't masked, otherwise only with a valid `X-Reveal-Token`. `workspace_id`
    is resolved from the enclosing route's path parameter of the same name."""
    return not masking_enabled(workspace_id) or token_valid(x_reveal_token)


RevealDep = Annotated[bool, Depends(names_revealed)]


# ---------------------------------------------------------------- disguise

_DIGITS = re.compile(r"\d+")


def pseudonym(applicant_id: str) -> str:
    """`A007` -> `CAND0007`. Derived from the applicant id (itself just an
    ingest sequence number, not PII) so the alias is the same in every view,
    every export and across re-solves."""
    match = _DIGITS.search(applicant_id)
    if match is None:
        return f"CAND-{applicant_id}"
    return f"CAND{int(match.group()):04d}"


def disguise_assignment(a: Assignment) -> Assignment:
    return a.model_copy(
        update={
            "full_name": pseudonym(a.applicant_id),
            "email": HIDDEN_EMAIL if a.email else "",
        }
    )


def disguise_report_row(row: dict[str, Any]) -> dict[str, Any]:
    """Disguise one validation-report row (the Rejected tab, the ingest
    report). These rows never got an applicant id, so the alias is the CSV
    line they came from. A blank stays blank — "missing name" has to keep
    reading as missing — and the message is scrubbed too, since e.g.
    INVALID_EMAIL quotes the offending address."""
    full_name = str(row.get("full_name") or "")
    email = str(row.get("email") or "")
    csv_row = row.get("csv_row") or row.get("row_number") or 0
    alias = f"ROW{int(csv_row):04d}"

    message = str(row.get("message") or "")
    # Longest first, so a name that contains the email's local part (or vice
    # versa) is not left half-replaced.
    for secret, cover in sorted(
        ((full_name, alias), (email, HIDDEN_EMAIL)), key=lambda p: -len(p[0])
    ):
        if secret:
            message = message.replace(secret, cover)

    return {
        **row,
        "full_name": alias if full_name else "",
        "email": HIDDEN_EMAIL if email else "",
        "message": message,
    }
