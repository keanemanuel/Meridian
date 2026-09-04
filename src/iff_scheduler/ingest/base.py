"""ApplicantSource protocol — the seam between ingest and its data sources.

Swapping the Google Sheets adapter for the CSV fallback (FR-01, SPEC.md §4.2
Stage 3) means providing a new class with this shape; nothing downstream of
`read_raw` needs to change.
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd


class ApplicantSource(Protocol):
    """Reads raw applicant rows exactly as exported, with no interpretation."""

    def read_raw(self) -> pd.DataFrame: ...
