"""CSV blob store: local `data/` or Cloudflare R2. Quota checked before PUT."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import boto3
from botocore.config import Config

GIB = 1024**3
MIB = 1024**2
DEFAULT_MAX_BYTES = 8 * GIB
DEFAULT_MAX_FILE = 20 * MIB

# ponytail: check-then-put race if two uploads at once; one password = one user.
# Distributed lock if multiple writers.


class QuotaError(Exception):
    """PUT would exceed max_bytes or max_file_bytes — nothing was written."""


_data_dir = Path(__file__).resolve().parent / "data"
_cfg: dict[str, Any] = {}


def configure(
    r2: Mapping[str, Any] | None = None,
    data_dir: Path | None = None,
) -> None:
    """Set R2 secrets and/or local data directory (tests pass a temp dir)."""
    global _cfg, _data_dir
    _cfg = {
        key: (value.strip() if isinstance(value, str) else value)
        for key, value in dict(r2 or {}).items()
    }
    if data_dir is not None:
        _data_dir = Path(data_dir)


def _max_bytes() -> int:
    return int(_cfg.get("max_bytes") or DEFAULT_MAX_BYTES)


def _max_file() -> int:
    return int(_cfg.get("max_file_bytes") or DEFAULT_MAX_FILE)


def r2_enabled() -> bool:
    """True when endpoint + access keys are present."""
    return bool(
        _cfg.get("access_key_id")
        and _cfg.get("secret_access_key")
        and _cfg.get("endpoint_url")
    )


def _safe(name: str) -> str:
    base = Path(name).name
    if not base.lower().endswith(".csv") or base.startswith("."):
        raise ValueError("only .csv names allowed")
    return base


def _client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=str(_cfg["endpoint_url"]).rstrip("/"),
        aws_access_key_id=str(_cfg["access_key_id"]),
        aws_secret_access_key=str(_cfg["secret_access_key"]),
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def _bucket() -> str:
    return str(_cfg.get("bucket") or "reweek")


def bundled_names() -> list[str]:
    """CSV filenames shipped in the local data directory (git)."""
    if not _data_dir.is_dir():
        return []
    return sorted(path.name for path in _data_dir.glob("*.csv"))


def _r2_list() -> list[tuple[str, int, datetime]]:
    client = _client()
    bucket = _bucket()
    out: list[tuple[str, int, datetime]] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket}
        if token:
            kwargs["ContinuationToken"] = token
        resp = client.list_objects_v2(**kwargs)
        for obj in resp.get("Contents") or []:
            key = str(obj["Key"])
            if not key.lower().endswith(".csv"):
                continue
            modified = obj["LastModified"]
            if modified.tzinfo is None:
                modified = modified.replace(tzinfo=timezone.utc)
            out.append((key, int(obj["Size"]), modified))
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
    return out


def list_objects() -> list[tuple[str, int]]:
    """Objects that count toward quota: R2 keys, or local csvs in local mode."""
    if r2_enabled():
        return [(name, size) for name, size, _ in _r2_list()]
    return [(name, (_data_dir / name).stat().st_size) for name in bundled_names()]


def list_inventory() -> list[tuple[str, int, datetime]]:
    """CSV rows `(name, size, updated)` newest first. R2 overwrites same-name local."""
    by_name: dict[str, tuple[int, datetime]] = {}
    if _data_dir.is_dir():
        for path in _data_dir.glob("*.csv"):
            stat = path.stat()
            by_name[path.name] = (
                stat.st_size,
                datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            )
    if r2_enabled():
        for name, size, modified in _r2_list():
            by_name[name] = (size, modified)
    return sorted(
        [(name, size, ts) for name, (size, ts) in by_name.items()],
        key=lambda row: row[2],
        reverse=True,
    )


def list_names() -> list[str]:
    """Filenames newest-updated first."""
    return [name for name, _, _ in list_inventory()]


def used_bytes() -> int:
    """Sum of object sizes that count toward quota."""
    return sum(size for _, size in list_objects())


def usage() -> tuple[int, int]:
    """Return `(used_bytes, max_bytes)`."""
    return used_bytes(), _max_bytes()


def put_csv(name: str, data: bytes) -> None:
    """Store a CSV. Raises QuotaError without writing if over cap."""
    name = _safe(name)
    size = len(data)
    max_file = _max_file()
    if size > max_file:
        raise QuotaError(f"file {size} bytes exceeds max_file_bytes {max_file}")
    sizes = dict(list_objects())
    used = sum(sizes.values())
    old = sizes.get(name, 0)
    projected = used - old + size
    cap = _max_bytes()
    if projected > cap:
        raise QuotaError(
            f"quota exceeded: {projected} bytes > {cap} (used {used})"
        )
    if r2_enabled():
        _client().put_object(
            Bucket=_bucket(),
            Key=name,
            Body=data,
            ContentType="text/csv",
        )
        return
    _data_dir.mkdir(parents=True, exist_ok=True)
    (_data_dir / name).write_bytes(data)


def get_csv(name: str) -> bytes:
    """Load CSV bytes; R2 wins over a same-named bundled file."""
    name = _safe(name)
    if r2_enabled():
        keys = {key for key, _, _ in _r2_list()}
        if name in keys:
            resp = _client().get_object(Bucket=_bucket(), Key=name)
            return bytes(resp["Body"].read())
    path = _data_dir / name
    if path.is_file():
        return path.read_bytes()
    raise FileNotFoundError(name)


def delete_csv(name: str) -> None:
    """Remove a CSV from R2 (if enabled) and from the local data dir."""
    name = _safe(name)
    if r2_enabled():
        _client().delete_object(Bucket=_bucket(), Key=name)
    path = _data_dir / name
    if path.is_file():
        path.unlink()
