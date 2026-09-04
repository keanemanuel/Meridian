"""Publish-time views over a solved schedule (SPEC.md §3.6, FR-50..FR-55).

Pure view builders (`room_view`, `applicant_view`, `panel_view`) live here
alongside the I/O writers (`xlsx_writer`, `html_writer`) that turn them into
files. The writers are adapters — this is why `export/` sits outside the
core per CLAUDE.md's "Architecture rule", even though the view builders
themselves touch no file handles.
"""

from __future__ import annotations
