"""Postgres/Supabase persistence layer (beta, SPEC.md §14).

The alpha store is CSV + YAML on disk (CLAUDE.md, "No database in alpha").
Beta swaps that for Supabase Postgres for the Vercel deployment. This package
holds the client singleton and one repo per table; every repo takes/returns
the same plain domain objects the core already produces, so the swap is
`CsvSource -> PostgresSource`, not a rewrite.

`supabase_enabled()` is the single switch: when `SUPABASE_URL` /
`SUPABASE_KEY` are unset (always, for the CLI) the API falls back to the
alpha file store and this package is never imported.
"""

from __future__ import annotations

from iff_scheduler.db.client import get_client, reset_client_cache, supabase_enabled

__all__ = ["get_client", "reset_client_cache", "supabase_enabled"]
