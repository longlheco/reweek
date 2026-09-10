"""Compare two CSVs with a column→formula map."""

from __future__ import annotations

import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Union

import pandas as pd

FORMULAS: dict[str, Callable[[float, float], float | None]] = {
    "pct_change": lambda n, o: None if o == 0 else (n - o) / o,
}
DEFAULT_FORMULA = "pct_change"
COLUMN_FORMULA: dict[str, str] = {}
SKIP_COLS = {"No", "Detail"}
JOIN_KEY = "ID"

Source = Union[Path, str, bytes, IO[bytes], pd.DataFrame]


@dataclass(frozen=True)
class CompareResult:
    table: pd.DataFrame
    target_label: str
    baseline_label: str
    growth_cols: tuple[str, ...]


def parse_num(value: object) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return None
    text = text.replace("\n", "").replace(",", "").strip().upper()
    text = re.sub(r"\s+[A-Z]+$", "", text)
    factor = 1.0
    if text.endswith("K"):
        factor = 1_000.0
        text = text[:-1]
    elif text.endswith("M"):
        factor = 1_000_000.0
        text = text[:-1]
    text = re.sub(r"[A-Z]+$", "", text).strip()
    try:
        return float(text) * factor
    except ValueError:
        return None


def label_from_name(name: str) -> str:
    return Path(name).stem


def load_week(src: Source) -> pd.DataFrame:
    if isinstance(src, pd.DataFrame):
        frame = src.copy()
    elif isinstance(src, bytes):
        frame = pd.read_csv(io.BytesIO(src), encoding="utf-8-sig")
    else:
        frame = pd.read_csv(src, encoding="utf-8-sig")
    if JOIN_KEY not in frame.columns:
        raise ValueError(f"missing {JOIN_KEY} column")
    frame[JOIN_KEY] = frame[JOIN_KEY].astype(str).str.strip()
    frame = frame[frame[JOIN_KEY] != "Total"].copy()
    frame = frame.drop(columns=[col for col in SKIP_COLS if col in frame.columns])
    for col in frame.columns:
        if col != JOIN_KEY:
            frame[col] = frame[col].map(parse_num)
    return frame


def _cell(frame: pd.DataFrame, pid: str, col: str) -> float | None:
    if pid not in frame.index or col not in frame.columns:
        return None
    value = frame.at[pid, col]
    if pd.isna(value):
        return None
    return float(value)


def compare(
    target: Source,
    baseline: Source,
    target_label: str,
    baseline_label: str,
) -> CompareResult:
    left = load_week(target).drop_duplicates(JOIN_KEY).set_index(JOIN_KEY)
    right = load_week(baseline).drop_duplicates(JOIN_KEY).set_index(JOIN_KEY)
    metrics: list[str] = [col for col in left.columns]
    for col in right.columns:
        if col not in metrics:
            metrics.append(col)
    growth_cols = tuple(f"Growth {col}" for col in metrics)
    rows: list[dict[str, object]] = []
    for pid in left.index.union(right.index):
        in_target = pid in left.index
        in_base = pid in right.index
        row: dict[str, object] = {JOIN_KEY: pid}
        for col in metrics:
            new = _cell(left, pid, col) if in_target else None
            old = _cell(right, pid, col) if in_base else None
            growth: float | None = None
            if new is not None and old is not None:
                formula = FORMULAS[COLUMN_FORMULA.get(col, DEFAULT_FORMULA)]
                growth = formula(new, old)
            row[f"{col} {target_label}"] = new
            row[f"{col} {baseline_label}"] = old
            row[f"Growth {col}"] = growth
        rows.append(row)
    table = pd.DataFrame(rows)
    ordered = [JOIN_KEY]
    for col in metrics:
        ordered.extend(
            [
                f"{col} {baseline_label}",
                f"{col} {target_label}",
                f"Growth {col}",
            ]
        )
    table = table[ordered]
    for col in ordered:
        if col == JOIN_KEY:
            continue
        table[col] = pd.to_numeric(table[col], errors="coerce")
    sort_col = f"{metrics[0]} {target_label}" if metrics else ""
    if sort_col in table.columns:
        table = table.sort_values(sort_col, ascending=False, na_position="last")
    return CompareResult(
        table=table.reset_index(drop=True),
        target_label=target_label,
        baseline_label=baseline_label,
        growth_cols=growth_cols,
    )
