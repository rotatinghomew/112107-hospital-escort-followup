"""CSV loading for the standard-library coordination project."""

from __future__ import annotations

import csv
from pathlib import Path

from .models import (
    Booking,
    ClosingDay,
    Commitment,
    Dataset,
    HandoverAuthorization,
    Household,
    IntakeRecord,
    Notice,
    Quote,
    Receipt,
    Route,
    Shift,
)
from .timeutil import on_date, parse_date, parse_dt


def _read(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _semicolon(value: str) -> frozenset[str]:
    return frozenset(item.strip() for item in (value or "").split(";") if item.strip())


def load_dataset(data_dir: str | Path = "data") -> Dataset:
    root = Path(data_dir)

    bookings: list[Booking] = []
    for row in _read(root / "bookings.csv"):
        day = parse_date(row["date"])
        bookings.append(
            Booking(
                booking=row["booking"],
                home=row["home"],
                date=day,
                clinic=row["clinic"],
                arrival_from=on_date(day, row["arrival_from"]),
                arrival_until=on_date(day, row["arrival_until"]),
                release_from=on_date(day, row["release_from"]),
                release_until=on_date(day, row["release_until"]),
                max_wait=int(row["max_wait"]),
                support=row["support"],
                expires=parse_dt(row["expires"]),  # type: ignore[arg-type]
            )
        )

    households = [
        Household(
            home=row["home"],
            origin=row["origin"],
            return_to=row["return_to"],
            following_site=row["following_site"] or None,
            next_latest=row["next_latest"] or None,
            requested_at=parse_dt(row["requested_at"]),  # type: ignore[arg-type]
        )
        for row in _read(root / "households.csv")
    ]

    shifts: list[Shift] = []
    for row in _read(root / "rides.csv"):
        day = parse_date(row["date"])
        shifts.append(
            Shift(
                shift=row["shift"],
                worker=row["worker"],
                date=day,
                start=on_date(day, row["from"]),
                end=on_date(day, row["until"]),
                locations=_semicolon(row["locations"]),
            )
        )

    routes = [
        Route(
            node_a=row["node_a"],
            node_b=row["node_b"],
            min_minutes=int(row["min_minutes"]),
            max_minutes=int(row["max_minutes"]),
        )
        for row in _read(root / "routes.csv")
    ]

    handovers = [
        HandoverAuthorization(
            id=row["id"],
            home=row["home"],
            location=row["location"],
            valid_from=parse_dt(row["valid_from"]),  # type: ignore[arg-type]
            valid_until=parse_dt(row["valid_until"]),  # type: ignore[arg-type]
            received_at=parse_dt(row["received_at"]),  # type: ignore[arg-type]
            status=row["status"],
        )
        for row in _read(root / "handover.csv")
    ]

    commitments = [
        Commitment(
            trip=row["trip"],
            home=row["home"],
            booking=row["booking"],
            role=row["role"],
            shift=row["shift"],
            start=parse_dt(row["from"]),  # type: ignore[arg-type]
            end=parse_dt(row["until"]),  # type: ignore[arg-type]
            status=row["status"],
        )
        for row in _read(root / "commitments.csv")
    ]

    notices = [
        Notice(
            booking=row["booking"],
            occurred_at=parse_dt(row["occurred_at"]),  # type: ignore[arg-type]
            noted_at=parse_dt(row["noted_at"]),  # type: ignore[arg-type]
            kind=row["kind"],
            related=row["related"] or None,
        )
        for row in _read(root / "appointment_notices.csv")
    ]

    closing_days = [
        ClosingDay(
            day=parse_date(row["day"]),
            cutoff=parse_dt(row["cutoff"]),  # type: ignore[arg-type]
            recorded_at=parse_dt(row["recorded_at"]),  # type: ignore[arg-type]
            unit=row["unit"],
            complete=row["complete"].strip().lower() in {"yes", "true", "1"},
        )
        for row in _read(root / "closing_days.csv")
    ]

    intake = [
        IntakeRecord(
            booking=row["booking"],
            known_at=parse_dt(row["known_at"]),  # type: ignore[arg-type]
            disposition=row["disposition"],
            related=row["related"] or None,
        )
        for row in _read(root / "intake_register.csv")
    ]

    receipts = [
        Receipt(
            receipt=row["receipt"],
            trip=row["trip"],
            booking=row["booking"],
            kind=row["kind"],
            location=row["location"],
            happened_at=parse_dt(row["happened_at"]),  # type: ignore[arg-type]
            reported_at=parse_dt(row["reported_at"]),  # type: ignore[arg-type]
            proof=row["proof"] or None,
        )
        for row in _read(root / "trip_receipts.csv")
    ]

    quotes = [
        Quote(
            plan=row["plan"],
            unit=row["unit"],
            weekday=int(row["weekday"]),
            start_hm=row["start"],
            end_hm=row["end"],
            locations=_semicolon(row["locations"]),
            shift_count=int(row["shift_count"]),
            cost_cents=int(row["cost_cents"]),
            valid_until=parse_dt(row["valid_until"]),  # type: ignore[arg-type]
        )
        for row in _read(root / "extra_shift_quotes.csv")
    ]

    return Dataset(
        root=root,
        bookings=bookings,
        households=households,
        shifts=shifts,
        routes=routes,
        handovers=handovers,
        commitments=commitments,
        notices=notices,
        closing_days=closing_days,
        intake=intake,
        receipts=receipts,
        quotes=quotes,
    )
