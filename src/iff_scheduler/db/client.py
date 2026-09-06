"""Supabase client singleton, built from the environment.

`supabase` is an optional dependency: it is imported lazily inside
`get_client()` so `import iff_scheduler.db` never fails on a machine that
only runs the CLI (which never sets `SUPABASE_*`).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from dotenv import load_dotenv

# `supabase` ships no type information; it is also an optional dependency, so
# it is never imported at module scope. `Client` is treated as `Any`.
Client = Any

_REQUIRED_ENV = ("SUPABASE_URL", "SUPABASE_KEY")


def supabase_enabled() -> bool:
    """True when both `SUPABASE_URL` and `SUPABASE_KEY` are set.

    The one switch the API uses to choose the Postgres backend over the alpha
    CSV/YAML store. The CLI never sets these, so it always stays on files
    (CLAUDE.md, "No database in alpha"); tests run file-mode unless they
    explicitly set the vars.
    """
    load_dotenv()
    return all(os.environ.get(name) for name in _REQUIRED_ENV)


@lru_cache(maxsize=1)
def get_client() -> Client:
    """Process-wide Supabase client, constructed once from the environment."""
    load_dotenv()
    missing = [name for name in _REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            f"Supabase backend requested but {', '.join(missing)} not set. "
            "Unset every SUPABASE_* var to use the file store instead."
        )
    from supabase import create_client  # lazy: optional dependency

    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])


def reset_client_cache() -> None:
    """Drop the cached client — for tests and credential rotation."""
    get_client.cache_clear()
