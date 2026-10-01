"""Small, centralized I/O helpers."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_table(frame: pd.DataFrame, csv_path: Path, parquet: bool = True) -> None:
    """Write a stable CSV and, when requested, a Parquet counterpart."""

    ensure_parent(csv_path)
    frame.to_csv(csv_path, index=False)
    if parquet:
        frame.to_parquet(csv_path.with_suffix(".parquet"), index=False)
