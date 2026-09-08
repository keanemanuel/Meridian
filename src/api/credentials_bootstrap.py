"""Bridge inline-JSON credential env vars to the file paths the core expects.

`iff_scheduler.ingest.sheets_source` and `iff_scheduler.notify.gmail_mailer`
both take a credentials file *path* — `GOOGLE_SERVICE_ACCOUNT_FILE`,
`GMAIL_OAUTH_CREDENTIALS`, `GMAIL_TOKEN_CACHE` (see `.env.example`) — because
alpha always ran on a machine with a real filesystem. Vercel's dashboard has
no place to upload a file, only env vars, so a deploy has to paste the JSON
itself. This module reconciles the two: if a var's value looks like JSON
rather than a path, it's written to a file under `/tmp` (writable on Vercel's
Python runtime) and the var is repointed at that file — every downstream
read still just sees a path.

Deliberately narrow and additive: an unset or path-shaped var is left alone,
so nothing changes for local dev or the CLI, which never see JSON here.
"""

from __future__ import annotations

import os
import tempfile

# Materialised once per cold start, under a name that's stable for the life
# of the container (so a re-import within the same process is a no-op).
_TMP_DIR = os.path.join(tempfile.gettempdir(), "meridian-credentials")

_JSON_SHAPED_VARS = (
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "GMAIL_OAUTH_CREDENTIALS",
    "GMAIL_TOKEN_CACHE",
)


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
            continue
        os.makedirs(_TMP_DIR, exist_ok=True)
        dest = os.path.join(_TMP_DIR, f"{name.lower()}.json")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(value)
        os.environ[name] = dest
