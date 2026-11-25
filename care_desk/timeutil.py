"""Time and interval helpers used by the coordination desk."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable, Tuple

TIMEZONE_NAME = "Asia/Shanghai"


def parse_dt(value: str | None) -> datetime | None:
    if value is None or value == "":
        return None
    return datetime.fromisoformat(value)


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def parse_hm(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":"))
    return time(hour=hour, minute=minute)


def on_date(day: date | str, hm: str) -> datetime:
    day_value = parse_date(day) if isinstance(day, str) else day
    return datetime.combine(day_value, parse_hm(hm))


def as_date_key(value: datetime) -> str:
    return value.date().isoformat()


def overlaps(
    start_a: datetime,
    end_a: datetime,
    start_b: datetime,
    end_b: datetime,
) -> bool:
    """Return whether two promised intervals overlap.

    Intervals touching exactly at an endpoint are adjacent rather than
    overlapping; travel feasibility between different nodes is checked
    separately by the scheduler.
    """
    return start_a < end_b and start_b < end_a


def latest_of(values: Iterable[datetime]) -> datetime:
    values = list(values)
    if not values:
        raise ValueError("latest_of() requires at least one value")
    return max(values)


def add_minutes(value: datetime, minutes: int) -> datetime:
    return value + timedelta(minutes=minutes)


def minute_range(start: int, end_inclusive: int) -> Iterable[int]:
    if end_inclusive < start:
        return ()
    return range(start, end_inclusive + 1)


def interval_text(start: datetime, end: datetime) -> str:
    return f"{start.isoformat(timespec='minutes')}/{end.isoformat(timespec='minutes')}"
