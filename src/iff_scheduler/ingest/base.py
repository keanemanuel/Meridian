"""ApplicantSource protocol — the seam between ingest and its data source.

A source provides a class with this shape; nothing downstream of `read_raw`
needs to change. `CsvApplicantSource` (a Google Form CSV export) is the only
supported input (FR-01, SPEC.md §4.2 Stage 3).
"""

from __future__ import annotations

from typing import Protocol

import pandas as pd


class ApplicantSource(Protocol):
    """Reads raw applicant rows exactly as exported, with no interpretation."""

    def read_raw(self) -> pd.DataFrame: ...
