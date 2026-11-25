"""Typed records for the synthetic coordination sources.

The CSV files remain the source of truth.  These objects intentionally keep
both the raw values and parsed temporal fields so reports can always show
what was registered and when.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .timeutil import on_date, parse_date, parse_dt


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class Booking:
    booking: str
    home: str
    date: date
    clinic: str
    arrival_from: datetime
    arrival_until: datetime
    release_from: datetime
    release_until: datetime
    max_wait: int
    support: str
    expires: datetime

    @property
    def crew_count(self) -> int:
        return 2 if self.support == "team" else 1


@dataclass(frozen=True)
class Household:
    home: str
    origin: str
    return_to: str
    following_site: str | None
    next_latest: str | None
    requested_at: datetime

    @property
    def next_latest_dt(self) -> datetime | None:
        if not self.next_latest:
            return None
        # The service date is supplied by the booking; callers use
        # ``deadline_for_date`` because this object has no date of its own.
        raise TypeError("Use deadline_for_date(service_date)")

    def deadline_for_date(self, day: date) -> datetime | None:
        return on_date(day, self.next_latest) if self.next_latest else None


@dataclass(frozen=True)
class Shift:
    shift: str
    worker: str
    date: date
    start: datetime
    end: datetime
    locations: frozenset[str]
    source: str = "rides"
    plan: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Route:
    node_a: str
    node_b: str
    min_minutes: int
    max_minutes: int


@dataclass(frozen=True)
class HandoverAuthorization:
    id: str
    home: str
    location: str
    valid_from: datetime
    valid_until: datetime
    received_at: datetime
    status: str


@dataclass(frozen=True)
class Commitment:
    trip: str
    home: str
    booking: str
    role: str
    shift: str
    start: datetime
    end: datetime
    status: str


@dataclass(frozen=True)
class Notice:
    booking: str
    occurred_at: datetime
    noted_at: datetime
    kind: str
    related: str | None


@dataclass(frozen=True)
class ClosingDay:
    day: date
    cutoff: datetime
    recorded_at: datetime
    unit: str
    complete: bool


@dataclass(frozen=True)
class IntakeRecord:
    booking: str
    known_at: datetime
    disposition: str
    related: str | None


@dataclass(frozen=True)
class Receipt:
    receipt: str
    trip: str
    booking: str
    kind: str
    location: str
    happened_at: datetime
    reported_at: datetime
    proof: str | None


@dataclass(frozen=True)
class Quote:
    plan: str
    unit: str
    weekday: int
    start_hm: str
    end_hm: str
    locations: frozenset[str]
    shift_count: int
    cost_cents: int
    valid_until: datetime

    def shifts_for(self, day: date) -> list[Shift]:
        start = on_date(day, self.start_hm)
        end = on_date(day, self.end_hm)
        shifts: list[Shift] = []
        for index in range(1, self.shift_count + 1):
            worker = f"QUOTE-{self.plan}-W{index}"
            shifts.append(
                Shift(
                    shift=f"{self.plan}#{index}",
                    worker=worker,
                    date=day,
                    start=start,
                    end=end,
                    locations=self.locations,
                    source="quote",
                    plan=self.plan,
                    extra={"shift_index": index},
                )
            )
        return shifts


@dataclass
class Dataset:
    root: Path
    bookings: list[Booking]
    households: list[Household]
    shifts: list[Shift]
    routes: list[Route]
    handovers: list[HandoverAuthorization]
    commitments: list[Commitment]
    notices: list[Notice]
    closing_days: list[ClosingDay]
    intake: list[IntakeRecord]
    receipts: list[Receipt]
    quotes: list[Quote]

    def booking(self, booking_id: str) -> Booking:
        for item in self.bookings:
            if item.booking == booking_id:
                return item
        raise KeyError(booking_id)

    def household(self, home: str) -> Household:
        for item in self.households:
            if item.home == home:
                return item
        raise KeyError(home)

    def routes_on(self, a: str, b: str) -> tuple[int, int] | None:
        if a == b:
            return (0, 0)
        for route in self.routes:
            if {route.node_a, route.node_b} == {a, b}:
                return route.min_minutes, route.max_minutes
        return None

    def closing_day(self, day: str | date) -> ClosingDay:
        target = parse_date(day) if isinstance(day, str) else day
        for item in self.closing_days:
            if item.day == target:
                return item
        raise KeyError(target.isoformat())
