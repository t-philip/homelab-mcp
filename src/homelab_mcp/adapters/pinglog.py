"""Ping / uptime-log adapter — read a local availability log file.

Works with a CSV (header row) or JSONL file where each row is one reachability
sample. Column names are configurable so it can sit on top of whatever a given
monitor writes.

Only the tail of the file is read (``max_bytes``). For CSV the header line is
read separately from the front of the file, so a large rotating log never has
to be read in full.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..errors import ConfigError, HomelabMcpError, UpstreamError
from .base import (
    Adapter,
    AdapterOptions,
    ProbeResult,
    ToolSpec,
    int_arg,
    opt_number,
    require_number,
)

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_TRUTHY = {"1", "true", "yes", "up", "ok", "success", "reachable"}
_FALSY = {"0", "false", "no", "down", "fail", "failure", "unreachable", "timeout"}
#: A numeric timestamp at or above this is milliseconds, not seconds
#: (1e11 seconds is the year 5138 — no ping log is dated then).
_EPOCH_MS_THRESHOLD = 1e11


def _from_epoch(value: float) -> datetime | None:
    if value != value:  # NaN
        return None
    if abs(value) >= _EPOCH_MS_THRESHOLD:
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (ValueError, OSError, OverflowError):
        return None


def _parse_time(value: Any, assume_tz: tzinfo) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _from_epoch(float(value))
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            return _from_epoch(float(value))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=assume_tz)


def _coerce_status(row: dict[str, Any], success_col: str, rtt_col: str) -> bool | None:
    if success_col in row and row[success_col] not in ("", None):
        raw = str(row[success_col]).strip().lower()
        if raw in _TRUTHY:
            return True
        if raw in _FALSY:
            return False
    if rtt_col in row:
        rtt = row[rtt_col]
        if rtt in ("", None):
            return False
        try:
            return float(rtt) >= 0
        except (TypeError, ValueError):
            return None
    return None


class PinglogAdapter(Adapter):
    name = "pinglog"
    required_options = ("path",)

    def __init__(self, options: AdapterOptions) -> None:
        super().__init__(options)
        self._path = Path(str(options.get("path", ""))).expanduser()
        self._format = str(options.get("format", "csv")).lower()
        self._time_col = str(options.get("time_column", "timestamp"))
        self._target_col = str(options.get("target_column", "target"))
        self._success_col = str(options.get("success_column", "success"))
        self._rtt_col = str(options.get("rtt_column", "rtt_ms"))
        self._max_bytes = int(opt_number(options, "max_bytes", _DEFAULT_MAX_BYTES, minimum=1024))
        self._tz = _resolve_tz(str(options.get("timezone", "UTC")))

    @classmethod
    def validate(cls, options: AdapterOptions) -> None:
        super().validate(options)
        fmt = str(options.get("format", "csv")).lower()
        if fmt not in ("csv", "jsonl"):
            raise ConfigError("[adapters.pinglog].format must be 'csv' or 'jsonl'")
        require_number(options, "max_bytes", minimum=1024)
        _resolve_tz(str(options.get("timezone", "UTC")))

    # -- reading ---------------------------------------------------

    def _tail_bytes(self) -> tuple[bytes, bool]:
        try:
            with self._path.open("rb") as fh:
                fh.seek(0, io.SEEK_END)
                size = fh.tell()
                trimmed = size > self._max_bytes
                fh.seek(size - self._max_bytes if trimmed else 0)
                return fh.read(), trimmed
        except OSError as exc:
            raise UpstreamError(f"cannot read ping log {self._path}: {exc}") from exc

    def _csv_header(self) -> list[str] | None:
        try:
            with self._path.open("r", encoding="utf-8", errors="replace") as fh:
                first = fh.readline()
        except OSError as exc:  # pragma: no cover - _tail_bytes already validated
            raise UpstreamError(f"cannot read ping log {self._path}: {exc}") from exc
        for record in csv.reader(io.StringIO(first)):
            return record
        return None

    def _rows(self) -> Iterator[dict[str, Any]]:
        raw, trimmed = self._tail_bytes()
        text = raw.decode("utf-8", errors="replace")

        if self._format == "jsonl":
            for jline in text.splitlines():
                line = jline.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    yield obj
            return

        if not trimmed:
            yield from csv.DictReader(io.StringIO(text))
            return
        # Trimmed tail: no header in it. Read the header from the front, drop the
        # partial first line of the tail, and parse the rest against that header.
        header = self._csv_header()
        if not header or self._time_col not in header:
            raise UpstreamError(
                f"{self._path.name}: could not read a usable CSV header "
                f"(need a '{self._time_col}' column)"
            )
        body = text.split("\n", 1)[-1]
        yield from csv.DictReader(io.StringIO(body), fieldnames=header)

    async def _cached_samples(self) -> list[tuple[datetime, str, bool | None]]:
        try:
            stat = self._path.stat()
        except OSError as exc:
            raise UpstreamError(f"cannot stat ping log {self._path}: {exc}") from exc
        key = ("pinglog", str(self._path), stat.st_mtime_ns, stat.st_size)

        async def _load() -> list[tuple[datetime, str, bool | None]]:
            out: list[tuple[datetime, str, bool | None]] = []
            for row in self._rows():
                ts = _parse_time(row.get(self._time_col), self._tz)
                target = row.get(self._target_col)
                if ts is None or not target:
                    continue
                out.append((ts, str(target), _coerce_status(row, self._success_col, self._rtt_col)))
            return out

        return await self.options.cache.get_or_call(key, _load)

    # -- probe / tools -------------------------------------------

    async def probe(self) -> ProbeResult:
        if not self._path.is_file():
            return ProbeResult(self.name, False, f"{self._path} does not exist")
        try:
            samples = await self._cached_samples()
        except HomelabMcpError as exc:
            return ProbeResult(self.name, False, str(exc))
        if not samples:
            return ProbeResult(self.name, False, f"{self._path}: no parseable rows")
        latest = max(s[0] for s in samples)
        unknown = sum(1 for _, _, st in samples if st is None)
        detail = f"{self._path.name}: {len(samples)} samples, newest {latest.isoformat()}"
        if unknown == len(samples):
            return ProbeResult(
                self.name,
                False,
                detail + " — every sample has unknown status; check success_column / rtt_column",
            )
        if unknown > len(samples) // 2:
            detail += f" ({unknown} of {len(samples)} have unknown status — check column names)"
        return ProbeResult(self.name, True, detail)

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "pinglog_summary",
                "Per-target availability over the last N minutes: sample count, "
                "up/down counts, uptime %, last status.",
                self.pinglog_summary,
            ),
            ToolSpec(
                "pinglog_target_history",
                "The most recent raw samples for one target.",
                self.pinglog_target_history,
            ),
        ]

    async def pinglog_summary(self, window_minutes: int = 60) -> list[dict[str, Any]]:
        """Per-target availability over a recent window.

        Args:
            window_minutes: how far back to summarise (1-10080).
        """
        window_minutes = int_arg(window_minutes, "window_minutes", lo=1, hi=7 * 24 * 60)
        cutoff = datetime.now(tz=UTC).timestamp() - window_minutes * 60
        by_target: dict[str, dict[str, Any]] = {}
        for ts, target, status in await self._cached_samples():
            if ts.timestamp() < cutoff:
                continue
            agg = by_target.setdefault(
                target, {"target": target, "samples": 0, "up": 0, "down": 0, "unknown": 0}
            )
            agg["samples"] += 1
            if status is True:
                agg["up"] += 1
            elif status is False:
                agg["down"] += 1
            else:
                agg["unknown"] += 1
            last = agg.get("_last_ts")
            if last is None or ts > last:
                agg["_last_ts"] = ts
                agg["last_seen"] = ts.isoformat()
                agg["last_status"] = (
                    "up" if status is True else "down" if status is False else "unknown"
                )

        result: list[dict[str, Any]] = []
        for agg in by_target.values():
            agg.pop("_last_ts", None)
            decided = agg["up"] + agg["down"]
            agg["uptime_pct"] = round(100.0 * agg["up"] / decided, 1) if decided else None
            result.append(agg)
        return sorted(result, key=lambda a: a["target"])

    async def pinglog_target_history(self, target: str, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent samples for one target.

        Args:
            target: the target name as it appears in the log.
            limit: how many trailing samples to return (1-500).
        """
        n = int_arg(limit, "limit", lo=1, hi=500)
        # `_cached_samples` yields rows in file order, which is not guaranteed to
        # be chronological (a monitor may prepend, or the file may be
        # concatenated after rotation). Sort so "most recent" really is.
        recent = sorted(
            ((ts, st) for ts, tgt, st in await self._cached_samples() if tgt == target),
            key=lambda pair: pair[0],
        )[-n:]
        return [
            {
                "time": ts.isoformat(),
                "status": "up" if st is True else "down" if st is False else "unknown",
            }
            for ts, st in recent
        ]


def _resolve_tz(name: str) -> tzinfo:
    if name.upper() == "UTC":
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"[adapters.pinglog].timezone {name!r} is not a known IANA zone") from exc
