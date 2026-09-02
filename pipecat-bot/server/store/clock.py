"""Injected time. No module in this package calls datetime.now() directly.

A Clock always returns a timezone-aware datetime. Production uses IstClock;
tests use FixedClock so return-window math is deterministic.
"""

from datetime import UTC, datetime, timezone
from typing import Protocol, runtime_checkable
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


class IstClock:
    """Wall-clock time, IST. (Persistence converts to UTC at the boundary.)"""

    def now(self) -> datetime:
        return datetime.now(tz=IST)


class FixedClock:
    """A clock pinned to one instant, for tests."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None or at.tzinfo.utcoffset(at) is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._at = at

    def now(self) -> datetime:
        return self._at


def to_utc_iso(dt: datetime) -> str:
    """Persistence boundary: aware datetime -> UTC ISO-8601 string."""
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError("refusing to persist a naive datetime")
    return dt.astimezone(UTC).isoformat()


def from_utc_iso(s: str) -> datetime:
    """Persistence boundary: stored string -> aware datetime (UTC)."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"stored datetime is naive: {s!r}")
    return dt.astimezone(UTC)
