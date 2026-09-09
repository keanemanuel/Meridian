"""Bridge inline-JSON credential env vars to the file paths the core expects.

`iff_scheduler.ingest.sheets_source`, `iff_scheduler.export.sheets_writer` and
`iff_scheduler.notify.gmail_mailer` all take a credentials file *path* —
`GOOGLE_SERVICE_ACCOUNT_FILE`, `GMAIL_OAUTH_CREDENTIALS`, `GMAIL_TOKEN_CACHE`
(see `.env.example`) — because alpha always ran on a machine with a real
filesystem. A hosting dashboard has no place to upload a file, only env vars,
so a deploy has to paste the JSON itself. This module reconciles the two: if
a var's value looks like JSON rather than a path, it's written to a file under
`/tmp` and the var is repointed at that file — every downstream read still
just sees a path.

Deliberately narrow and additive: an unset or path-shaped var is left alone,
so nothing changes for local dev or the CLI, which never see JSON here.

**Never raises.** This runs at import in `api/main.py`, so a bad value must
not take the whole service down with it — `/api/health` has to keep
answering or the platform restarts the container in a loop. A value that
cannot be used is recorded in `credential_problem()` instead, and the
endpoint that needs it turns that into an actionable error at the point of
use (`api.dependencies.service_account_file`).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass

# Materialised once per cold start, under a name that's stable for the life
# of the container (so a re-import within the same process is a no-op).
_TMP_DIR = os.path.join(tempfile.gettempdir(), "meridian-credentials")

_JSON_SHAPED_VARS = (
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "GMAIL_OAUTH_CREDENTIALS",
    "GMAIL_TOKEN_CACHE",
)


@dataclass(frozen=True)
class CredentialProblem:
    """Why one credential env var could not be turned into a usable file.

    `message` is written for whoever pastes the value into the deploy
    dashboard, so it names the variable and says what to do — never just
    what json.loads happened to say.
    """

    variable: str
    message: str


_PROBLEMS: dict[str, CredentialProblem] = {}


def credential_problem(variable: str) -> CredentialProblem | None:
    """The problem recorded for `variable` at startup, if any."""
    return _PROBLEMS.get(variable)


def _json_documents(value: str) -> list[str]:
    """Every complete JSON value in `value`, in order.

    Raises `json.JSONDecodeError` if anything between or after them is not
    itself a JSON value. `raw_decode` is driven by an index into the original
    string rather than over a trimmed copy, so the line and column on that
    error point at the real spot in the pasted value — the whole point of the
    message the operator has to act on.
    """
    decoder = json.JSONDecoder()
    documents: list[str] = []
    position, end_of_value = 0, len(value)

    while True:
        while position < end_of_value and value[position].isspace():
            position += 1
        if position >= end_of_value:
            return documents
        _, position_after = decoder.raw_decode(value, position)
        documents.append(value[position:position_after])
        position = position_after


def _canonical(document: str) -> str:
    return json.dumps(json.loads(document), sort_keys=True)


def usable_json(variable: str, value: str) -> str | None:
    """The one JSON document `value` holds, or None with a problem recorded.

    A value pasted twice is the common deploy slip and is unambiguous — the
    copies are byte-identical once parsed, so the first one is used and the
    duplication is logged. Anything else trailing the JSON is *not*
    unambiguous (two different service accounts, a half-paste, a stray key)
    and is refused rather than guessed at (CLAUDE.md invariant 3).
    """
    try:
        documents = _json_documents(value)
    except json.JSONDecodeError as exc:
        _PROBLEMS[variable] = CredentialProblem(
            variable,
            f"{variable} is not valid JSON and is not a readable path. "
            f"The JSON parser stopped at line {exc.lineno}, column {exc.colno}: "
            f"{exc.msg}. Re-paste the whole credentials file as the value.",
        )
        return None

    if not documents:
        _PROBLEMS[variable] = CredentialProblem(variable, f"{variable} is set but empty.")
        return None

    canonical = {_canonical(d) for d in documents}
    if len(canonical) > 1:
        _PROBLEMS[variable] = CredentialProblem(
            variable,
            f"{variable} contains {len(documents)} different JSON documents. "
            "Which one is current cannot be guessed. Set the value to exactly "
            "one credentials file and redeploy.",
        )
        return None

    if len(documents) > 1:
        print(
            f"{variable}: the same JSON document was pasted {len(documents)} times; "
            "using the first copy. Fix the value in the deploy dashboard to stop "
            "this warning.",
            file=sys.stderr,
        )
    _PROBLEMS.pop(variable, None)
    return documents[0]


def materialize_json_credentials() -> None:
    """For each var above, if its value is inline JSON, write it to a temp
    file and reassign the var to that file's path.

    A path-shaped value (the local/CLI case) starts with something other
    than `{`, so it's left untouched — this only ever rewrites values that
    could not have worked as a path anyway.
    """
    for name in _JSON_SHAPED_VARS:
        value = os.environ.get(name)
        if not value or not value.lstrip().startswith("{"):
            _PROBLEMS.pop(name, None)
            continue
        document = usable_json(name, value)
        if document is None:
            # Leave the variable pointing at the unusable JSON. The endpoint
            # that needs it reports `credential_problem(name)`; blanking it
            # here would only turn a precise error into "not set".
            continue
        os.makedirs(_TMP_DIR, exist_ok=True)
        dest = os.path.join(_TMP_DIR, f"{name.lower()}.json")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(document)
        os.environ[name] = dest
