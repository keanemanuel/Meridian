"""Manual adjustment model: locks and the edit validator (SPEC.md §11, FR-40..FR-45).

Part of the pure core alongside `domain/` and `scheduling/` — no adapter
imports, no I/O, no network (CLAUDE.md "Architecture rule"). Reading and
writing `data/locks/pinned_assignments.csv` is the CLI's job, same as it
already is for `assignments.csv`.
"""

from __future__ import annotations
