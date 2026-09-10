"""Compare + store quota checks."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import pytest

from compare import JOIN_KEY, compare, label_from_name
from store import QuotaError, configure, delete_csv, list_inventory, put_csv, r2_enabled

DATA = Path(__file__).resolve().parents[1] / "data"


def _sample_files() -> tuple[Path, Path]:
    files = list(DATA.glob("*.csv"))
    newer = next(path for path in files if "T9" in path.name)
    older = next(path for path in files if "T6" in path.name)
    return newer, older


def test_label_from_name() -> None:
    assert label_from_name("file A.csv") == "file A"
    assert label_from_name("file B.csv") == "file B"


def test_compare_a_vs_b() -> None:
    file_a, file_b = _sample_files()
    result = compare(file_a, file_b, "A", "B")
    row = result.table.loc[result.table[JOIN_KEY] == "russel2312"].iloc[0]
    assert row["Click A"] == 286668
    assert row["Click B"] == 305143
    assert round(float(row["Growth Click"]) * 100, 2) == -6.05
    assert round(float(row["Total Commission A"]), 2) == 17862.33
    assert round(float(row["Total Commission B"]), 2) == 13176.67
    assert round(float(row["Growth Total Commission"]) * 100, 2) == 35.56
    cols = list(result.table.columns)
    assert cols.index("Click B") < cols.index("Click A")
    swapped = compare(file_b, file_a, "B", "A")
    swapped_cols = list(swapped.table.columns)
    assert swapped_cols.index("Click A") < swapped_cols.index("Click B")


def test_missing_in_target_is_nan() -> None:
    file_a, file_b = _sample_files()
    result = compare(file_a, file_b, "A", "B")
    only_base = result.table[result.table["Click A"].isna()]
    assert not only_base.empty
    assert only_base["Growth Click"].isna().all()
    assert "russel2312" not in set(only_base[JOIN_KEY])


def test_baseline_zero_growth_none() -> None:
    target = pd.DataFrame(
        {
            "No": [1],
            "ID": ["zeroed"],
            "Click": [10],
            "Total Order": [1],
            "Total Commission": ["1.00 PHP"],
            "Sale Amount": ["1.00 PHP"],
            "Detail": ["Conversion"],
        }
    )
    baseline = pd.DataFrame(
        {
            "No": [1],
            "ID": ["zeroed"],
            "Click": [0],
            "Total Order": [0],
            "Total Commission": ["0 PHP"],
            "Sale Amount": ["0 PHP"],
            "Detail": ["Conversion"],
        }
    )
    result = compare(target, baseline, "A", "B")
    row = result.table.iloc[0]
    assert pd.isna(row["Growth Click"])


def test_r2_off_until_keys(tmp_path: Path) -> None:
    configure(
        {
            "endpoint_url": "https://example.r2.cloudflarestorage.com",
            "access_key_id": "  ",
            "secret_access_key": "secret",
            "bucket": "reweek",
        },
        tmp_path,
    )
    assert not r2_enabled()
    configure(
        {
            "endpoint_url": "https://example.r2.cloudflarestorage.com",
            "access_key_id": " id ",
            "secret_access_key": " secret ",
            "bucket": "reweek",
        },
        tmp_path,
    )
    assert r2_enabled()


def test_quota_rejects_without_writing(tmp_path: Path) -> None:
    configure({"max_bytes": 100, "max_file_bytes": 1000}, tmp_path)
    put_csv("a.csv", b"x" * 60)
    with pytest.raises(QuotaError):
        put_csv("b.csv", b"x" * 50)
    assert not (tmp_path / "b.csv").exists()
    put_csv("a.csv", b"x" * 40)
    assert (tmp_path / "a.csv").read_bytes() == b"x" * 40
    delete_csv("a.csv")
    assert not (tmp_path / "a.csv").exists()


def test_inventory_newest_first(tmp_path: Path) -> None:
    configure({}, tmp_path)
    put_csv("old.csv", b"old")
    put_csv("new.csv", b"new")
    os.utime(tmp_path / "old.csv", (1_000_000, 1_000_000))
    names = [name for name, _, _ in list_inventory()]
    assert names[0] == "new.csv"
    assert names[-1] == "old.csv"
