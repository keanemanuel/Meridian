"""CSV file adapter for ApplicantSource — the manual-export fallback path
(FR-01, SPEC.md §4.2 Stage 3: "File -> Download -> CSV, dropped into data/raw/").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class CsvApplicantSource:
    """Reads a raw Google Form CSV export.

    All columns are read as plain strings and blank cells stay as empty
    strings rather than becoming pandas NaN — otherwise a blank note or phone
    number would later render as the literal text "nan" in an email merge
    field (SPEC.md §10.2 step 3 names this failure mode explicitly).
    """

    path: Path

    def read_raw(self) -> pd.DataFrame:
        return pd.read_csv(self.path, dtype=str, keep_default_na=False, na_filter=False)
