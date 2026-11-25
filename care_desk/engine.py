"""Core coordination, atomic whole-trip reservations, and day replay."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Any, Iterable

from .models import Booking, Commitment, Dataset, Household, Quote, Shift
from .timeutil import add_minutes, overlaps


@dataclass
class BusyBlock:
    worker: str
    start: datetime
    end: datetime
    start_node: str | None = None
    end_node: str | None = None
    owner: str = "fixed"
    ref: str = ""


@dataclass
class Assignment:
    role: str
    start_node: str
    end_node: str
    start: datetime
    end: datetime
    shift: str
    worker: str
    source: str
    plan: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["start"] = self.start.isoformat(timespec="minutes")
        data["end"] = self.end.isoformat(timespec="minutes")
        return data


@dataclass
class Demand:
    home: str
    day: str
    household: Household
    options: list[Booking]
    requested_at: datetime
    expires: datetime
    priority_key: tuple[datetime, datetime, str]

    @property
    def key(self) -> str:
        return f"{self.home}@{self.day}"


@dataclass
class PlanResult:
    ok: bool
    demand_key: str
    home: str
    day: str
    status: str
    priority: dict[str, str] = field(default_factory=dict)
    selected_booking: str | None = None
    trip_kind: str | None = None
    hold_until: str | None = None
    assignments: list[Assignment] = field(default_factory=list)
    relay: dict[str, Any] | None = None
    reasons: list[dict[str, str]] = field(default_factory=list)
    already_confirmed_trip: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "demand_key": self.demand_key,
            "home": self.home,
            "day": self.day,
            "status": self.status,
            "priority": self.priority,
            "selected_booking": self.selected_booking,
            "trip_kind": self.trip_kind,
            "hold_until": self.hold_until,
            "assignments": [item.to_dict() for item in self.assignments],
            "relay": self.relay,
            "reasons": self.reasons,
            "already_confirmed_trip": self.already_confirmed_trip,
        }


class CareDesk:
    def __init__(self, dataset: Dataset):
        self.dataset = dataset

    # ------------------------------------------------------------------
    # Temporal visibility and demand grouping
    # ------------------------------------------------------------------
    def visible_notices(self, as_of: datetime | None):
        if as_of is None:
            return list(self.dataset.notices)
        return [n for n in self.dataset.notices if n.noted_at <= as_of]

    def cancelled_bookings(self, as_of: datetime | None = None) -> set[str]:
        return {
            notice.booking
            for notice in self.visible_notices(as_of)
            if notice.kind == "cancelled"
        }

    def visible_intake(self, as_of: datetime | None):
        if as_of is None:
            return list(self.dataset.intake)
        return [row for row in self.dataset.intake if row.known_at <= as_of]

    def confirmed_trip_for(self, booking_id: str, as_of: datetime | None) -> str | None:
        for row in self.visible_intake(as_of):
            if row.booking == booking_id and row.disposition == "confirmed" and row.related:
                return row.related
        # When no as-of is supplied, the commitments themselves are useful.
        if as_of is None:
            for commitment in self.dataset.commitments:
                if commitment.booking == booking_id and commitment.status == "confirmed":
                    return commitment.trip
        return None

    def demands_for_day(
        self,
        day: str,
        as_of: datetime | None = None,
        *,
        include_expired: bool = False,
    ) -> list[Demand]:
        from .timeutil import parse_date

        target = parse_date(day) if isinstance(day, str) else day
        cancelled = self.cancelled_bookings(as_of)
        grouped: dict[str, list[Booking]] = {}
        for booking in self.dataset.bookings:
            if booking.date != target:
                continue
            if booking.booking in cancelled:
                # Cancelled alternatives are retained in source data but are
                # not active options for a new atomic reservation.
                continue
            if as_of is not None and booking.expires <= as_of and not include_expired:
                continue
            grouped.setdefault(booking.home, []).append(booking)

        demands: list[Demand] = []
        for home, options in grouped.items():
            household = self.dataset.household(home)
            if as_of is not None and household.requested_at > as_of:
                continue
            earliest_expiry = min(item.expires for item in options)
            demands.append(
                Demand(
                    home=home,
                    day=target.isoformat(),
                    household=household,
                    options=sorted(options, key=lambda item: item.booking),
                    requested_at=household.requested_at,
                    expires=earliest_expiry,
                    priority_key=(earliest_expiry, household.requested_at, home),
                )
            )
        return sorted(demands, key=lambda item: item.priority_key)

    def effective_authorizations(
        self, home: str, as_of: datetime | None = None
    ) -> dict[str, HouseholdAuth]:
        rows = [row for row in self.dataset.handovers if row.home == home]
        if as_of is not None:
            rows = [row for row in rows if row.received_at <= as_of]
        latest: dict[str, Any] = {}
        for index, row in enumerate(sorted(rows, key=lambda r: (r.received_at, r.id))):
            # Later file order wins if received_at ties.
            latest[row.id] = (index, row)
        result: dict[str, HouseholdAuth] = {}
        for auth_id, (_, row) in latest.items():
            if row.status == "approved":
                result[auth_id] = row
        return result

    # ------------------------------------------------------------------
    # Shifts and fixed commitments
    # ------------------------------------------------------------------
    def shifts_for_day(
        self, day: str, quote_plans: Iterable[str] | None = None
    ) -> list[Shift]:
        from .timeutil import parse_date

        target = parse_date(day) if isinstance(day, str) else day
        shifts = [shift for shift in self.dataset.shifts if shift.date == target]
        if quote_plans:
            wanted = set(quote_plans)
            for quote in self.dataset.quotes:
                if quote.plan in wanted:
                    shifts.extend(quote.shifts_for(target))
        return shifts

    def _commitment_nodes(self, commitment: Commitment) -> tuple[str | None, str | None]:
        try:
            booking = self.dataset.booking(commitment.booking)
            household = self.dataset.household(commitment.home)
        except KeyError:
            return None, None
        if commitment.role == "out":
            return household.origin, booking.clinic
        if commitment.role == "follow":
            return booking.clinic, household.following_site
        if commitment.role == "return":
            if household.following_site:
                return household.following_site, household.return_to
            return booking.clinic, household.return_to
        return None, None

    def fixed_busy_blocks(
        self, day: str, as_of: datetime | None = None
    ) -> list[BusyBlock]:
        from .timeutil import parse_date

        target = parse_date(day) if isinstance(day, str) else day
        visible_trips: set[str] = set()
        for row in self.visible_intake(as_of):
            if row.disposition == "confirmed" and row.related:
                visible_trips.add(row.related)

        blocks: list[BusyBlock] = []
        cancelled = self.cancelled_bookings(as_of)
        for commitment in self.dataset.commitments:
            if commitment.start.date() != target:
                continue
            if as_of is not None and commitment.trip not in visible_trips:
                continue
            if commitment.booking in cancelled:
                # The cancellation is visible at as_of; only this invalid
                # scope is released. Other commitments remain in busy.
                continue
            if commitment.status != "confirmed":
                continue
            start_node, end_node = self._commitment_nodes(commitment)
            blocks.append(
                BusyBlock(
                    worker=self._worker_for_shift(commitment.shift),
                    start=commitment.start,
                    end=commitment.end,
                    start_node=start_node,
                    end_node=end_node,
                    owner="confirmed",
                    ref=f"{commitment.trip}/{commitment.role}",
                )
            )
        return blocks

    def _worker_for_shift(self, shift_id: str) -> str:
        for shift in self.dataset.shifts:
            if shift.shift == shift_id:
                return shift.worker
        return shift_id

    def cancellation_scopes(self, as_of: datetime | None = None) -> list[dict[str, Any]]:
        """Mark cancelled booking/role scope without releasing other promises."""
        scopes: list[dict[str, Any]] = []
        for notice in self.visible_notices(as_of):
            if notice.kind != "cancelled":
                continue
            try:
                booking = self.dataset.booking(notice.booking)
            except KeyError:
                continue
            affected = [
                c
                for c in self.dataset.commitments
                if c.booking == booking.booking and c.status == "confirmed"
            ]
            same_day_household = [
                c
                for c in self.dataset.commitments
                if c.home == booking.home
                and c.start.date() == booking.date
                and c not in affected
            ]
            scopes.append(
                {
                    "booking": booking.booking,
                    "home": booking.home,
                    "date": booking.date.isoformat(),
                    "occurred_at": notice.occurred_at.isoformat(timespec="minutes"),
                    "noted_at": notice.noted_at.isoformat(timespec="minutes"),
                    "invalid_scope": (
                        [f"{c.trip}:{c.role}" for c in affected]
                        if affected
                        else [f"booking_option:{booking.booking}"]
                    ),
                    "affected_trips": sorted({c.trip for c in affected}),
                    "remaining_valid_commitments": [
                        f"{c.trip}:{c.role}" for c in same_day_household
                    ],
                    "remaining_promise_rule": (
                        "其余有效承诺继续占用；在家庭同意整段改签前不得改派给其他家庭"
                    ),
                    "whole_rebooking_consent_required": True,
                }
            )
        return scopes

    def pending_verification_cases(self, as_of: datetime | None = None) -> list[dict[str, Any]]:
        """List arrived trips lacking visible valid final-handover proof."""
        cases: list[dict[str, Any]] = []
        from .outcomes import OutcomeAnalyzer

        outcome = OutcomeAnalyzer(self.dataset).actual_outcomes(
            as_of.isoformat(timespec="minutes") if as_of else None
        )
        for booking in outcome["bookings"]:
            pending = [
                c
                for c in booking["checkpoints"]
                if c.get("status") == "missing_proof_pending"
            ]
            arrived = any(
                c.get("point_type") == "hospital" and c.get("actual_at")
                for c in booking["checkpoints"]
            )
            if pending and arrived:
                cases.append(
                    {
                        "booking": booking["booking"],
                        "trip": booking["trip"],
                        "pending_points": pending,
                        "outbound_redispatch": "forbidden_until_verification_or_whole_change",
                        "rule": "已到医院但缺末段交接凭据：保持待核，不重新派去程；迟来有效签收可补完",
                    }
                )
        return cases

    # ------------------------------------------------------------------
    # Atomic planning
    # ------------------------------------------------------------------
    def plan_day(
        self,
        day: str,
        as_of: datetime | None = None,
        quote_plans: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        demands = self.demands_for_day(day, as_of=as_of)
        shifts = self.shifts_for_day(day, quote_plans=quote_plans)
        busy = self.fixed_busy_blocks(day, as_of=as_of)
        results: list[PlanResult] = []

        for demand in demands:
            confirmed_trip = None
            for booking in demand.options:
                confirmed_trip = self.confirmed_trip_for(booking.booking, as_of)
                if confirmed_trip:
                    break
            if confirmed_trip:
                results.append(
                    PlanResult(
                        ok=True,
                        demand_key=demand.key,
                        home=demand.home,
                        day=demand.day,
                        status="already_confirmed",
                        priority=self._priority_dict(demand),
                        selected_booking=next(
                            (b.booking for b in demand.options if self.confirmed_trip_for(b.booking, as_of)),
                            demand.options[0].booking,
                        ),
                        hold_until=demand.expires.isoformat(timespec="minutes"),
                        already_confirmed_trip=confirmed_trip,
                    )
                )
                continue

            plan = self.find_complete_plan(demand, shifts, busy, as_of=as_of)
            if plan.ok:
                plan.status = "reserved_complete"
                results.append(plan)
                for assignment in plan.assignments:
                    busy.append(
                        BusyBlock(
                            worker=assignment.worker,
                            start=assignment.start,
                            end=assignment.end,
                            start_node=assignment.start_node,
                            end_node=assignment.end_node,
                            owner=demand.key,
                            ref=plan.selected_booking or demand.key,
                        )
                    )
            else:
                plan.status = "unable_complete"
                results.append(plan)

        return {
            "day": demands[0].day if demands else day,
            "as_of": as_of.isoformat(timespec="minutes") if as_of else None,
            "quote_plans": sorted(set(quote_plans or [])),
            "shifts": [
                {
                    "shift": s.shift,
                    "worker": s.worker,
                    "start": s.start.isoformat(timespec="minutes"),
                    "end": s.end.isoformat(timespec="minutes"),
                    "locations": sorted(s.locations),
                    "source": s.source,
                    "plan": s.plan,
                }
                for s in shifts
            ],
            "results": [item.to_dict() for item in results],
            "cancellation_scopes": self.cancellation_scopes(as_of),
            "pending_verification": self.pending_verification_cases(as_of),
        }

    @staticmethod
    def _priority_dict(demand: Demand) -> dict[str, str]:
        return {
            "earliest_expires": demand.expires.isoformat(timespec="minutes"),
            "requested_at": demand.requested_at.isoformat(timespec="minutes"),
            "order": f"{demand.expires.isoformat(timespec='minutes')} < {demand.requested_at.isoformat(timespec='minutes')} < {demand.home}",
        }

    def find_complete_plan(
        self,
        demand: Demand,
        shifts: list[Shift],
        busy: list[BusyBlock],
        as_of: datetime | None = None,
    ) -> PlanResult:
        aggregate_reasons: list[dict[str, str]] = []
        authorizations = self.effective_authorizations(demand.home, as_of)

        for booking in demand.options:
            variants = self._variants(demand, booking, authorizations)
            for variant in variants:
                candidate = self._solve_variant(
                    demand, booking, variant, shifts, busy
                )
                if candidate.ok:
                    candidate.priority = self._priority_dict(demand)
                    candidate.hold_until = demand.expires.isoformat(timespec="minutes")
                    return candidate
                aggregate_reasons.extend(candidate.reasons)

        # De-duplicate reason codes; minute-by-minute candidates otherwise make
        # reports noisy without changing the coordination conclusion.
        unique_reasons: list[dict[str, str]] = []
        seen_codes: set[str] = set()
        for reason in aggregate_reasons:
            code = reason.get("code", "")
            if code and code in seen_codes:
                continue
            seen_codes.add(code)
            unique_reasons.append(reason)
        if not unique_reasons:
            unique_reasons.append(
                {"code": "no_complete_trip", "message": "找不到覆盖出门、交接和返程的整段安排"}
            )
        return PlanResult(
            ok=False,
            demand_key=demand.key,
            home=demand.home,
            day=demand.day,
            status="unable_complete",
            priority=self._priority_dict(demand),
            reasons=unique_reasons[:12],
        )

    def _variants(
        self,
        demand: Demand,
        booking: Booking,
        authorizations: dict[str, Any],
    ) -> list[dict[str, Any]]:
        h = demand.household
        variants: list[dict[str, Any]] = []

        clinic_relay = None
        following_relay = None
        for auth in authorizations.values():
            if auth.location == booking.clinic and self._auth_covers_release(auth, booking):
                clinic_relay = auth
            if h.following_site and auth.location == h.following_site:
                route = self.dataset.routes_on(booking.clinic, h.following_site)
                if route:
                    rmin, rmax = route
                    earliest = add_minutes(booking.release_from, rmin)
                    latest = add_minutes(booking.release_until, rmax)
                    if auth.valid_from <= earliest and auth.valid_until >= latest:
                        following_relay = auth

        if clinic_relay:
            variants.append(
                {
                    "kind": "outbound_then_authorized_relay",
                    "relay": clinic_relay,
                    "legs": ["outbound", "relay_clinic"],
                }
            )

        if h.following_site:
            variants.append(
                {
                    "kind": "outbound_follow_return_home",
                    "relay": None,
                    "legs": ["outbound", "follow", "return"],
                }
            )
            variants.append(
                {
                    "kind": "outbound_follow_handover",
                    "relay": following_relay,
                    "legs": ["outbound", "follow"],
                }
            )
        else:
            variants.append(
                {
                    "kind": "outbound_return_home",
                    "relay": None,
                    "legs": ["outbound", "return"],
                }
            )
        return variants

    @staticmethod
    def _auth_covers_release(auth: Any, booking: Booking) -> bool:
        return auth.valid_from <= booking.release_from and auth.valid_until >= booking.release_until

    def _solve_variant(
        self,
        demand: Demand,
        booking: Booking,
        variant: dict[str, Any],
        shifts: list[Shift],
        busy: list[BusyBlock],
    ) -> PlanResult:
        reasons: list[dict[str, str]] = []
        solved = self._select_leg_sequence(
            remaining_legs=list(variant["legs"]),
            shifts=shifts,
            busy=list(busy),
            chosen=[],
            demand=demand,
            booking=booking,
            variant=variant,
            reasons=reasons,
            previous_end=None,
        )
        if solved is None:
            return PlanResult(
                ok=False,
                demand_key=demand.key,
                home=demand.home,
                day=demand.day,
                status="unable_complete",
                selected_booking=booking.booking,
                trip_kind=variant["kind"],
                reasons=reasons[-8:]
                or [{"code": "crew_conflict", "message": "无法形成完整且不冲突的整段安排"}],
            )

        chosen, _temp_busy, _previous_end = solved
        relay = variant.get("relay")
        return PlanResult(
            ok=True,
            demand_key=demand.key,
            home=demand.home,
            day=demand.day,
            status="reserved_complete",
            priority={},
            selected_booking=booking.booking,
            trip_kind=variant["kind"],
            assignments=chosen,
            relay=(
                {
                    "authorization": relay.id,
                    "location": relay.location,
                    "valid_from": relay.valid_from.isoformat(timespec="minutes"),
                    "valid_until": relay.valid_until.isoformat(timespec="minutes"),
                    "status": relay.status,
                }
                if relay is not None
                else None
            ),
        )

    def _select_leg_sequence(
        self,
        remaining_legs: list[str],
        shifts: list[Shift],
        busy: list[BusyBlock],
        chosen: list[Assignment],
        demand: Demand,
        booking: Booking,
        variant: dict[str, Any],
        reasons: list[dict[str, str]],
        previous_end: datetime | None,
    ):
        legs = [leg for leg in remaining_legs if not leg.startswith("relay")]
        if not legs:
            return chosen, busy, previous_end

        leg = legs[0]
        candidates = self._leg_candidates(
            demand,
            booking,
            leg,
            shifts,
            busy,
            previous_end=previous_end,
            reasons=reasons,
        )
        for candidate in candidates:
            new_busy = list(busy)
            new_chosen = list(chosen)
            for assignment in candidate:
                new_chosen.append(assignment)
                new_busy.append(
                    BusyBlock(
                        worker=assignment.worker,
                        start=assignment.start,
                        end=assignment.end,
                        start_node=assignment.start_node,
                        end_node=assignment.end_node,
                        owner="tentative",
                        ref=booking.booking,
                    )
                )
            result = self._select_leg_sequence(
                legs[1:],
                shifts,
                new_busy,
                new_chosen,
                demand,
                booking,
                variant,
                reasons,
                candidate[-1].end if candidate else previous_end,
            )
            if result is not None:
                return result
        return None

    def _leg_nodes(
        self, demand: Demand, booking: Booking, leg: str
    ) -> tuple[str, str] | None:
        h = demand.household
        if leg == "outbound":
            return h.origin, booking.clinic
        if leg == "follow":
            if not h.following_site:
                return None
            return booking.clinic, h.following_site
        if leg == "return":
            if h.following_site:
                return h.following_site, h.return_to
            return booking.clinic, h.return_to
        return None

    def _leg_candidates(
        self,
        demand: Demand,
        booking: Booking,
        leg: str,
        shifts: list[Shift],
        busy: list[BusyBlock],
        *,
        previous_end: datetime | None,
        reasons: list[dict[str, str]],
    ) -> list[list[Assignment]]:
        nodes = self._leg_nodes(demand, booking, leg)
        if nodes is None:
            reasons.append({"code": "unknown_leg", "message": f"{leg} 环节缺少地点"})
            return []
        start_node, end_node = nodes
        route = self.dataset.routes_on(start_node, end_node)
        if route is None:
            reasons.append(
                {
                    "code": "route_not_verified",
                    "message": f"{start_node}↔{end_node} 没有可承诺耗时范围",
                }
            )
            return []
        rmin, rmax = route
        crew_count = booking.crew_count

        intervals: list[tuple[datetime, datetime]] = []
        is_first_release_leg = leg == "follow" or (
            leg == "return" and not demand.household.following_site
        )
        if leg == "outbound":
            earliest_departure = add_minutes(booking.arrival_from, -rmin)
            latest_departure = add_minutes(booking.arrival_until, -rmax)
            if earliest_departure > latest_departure:
                reasons.append(
                    {
                        "code": "arrival_window_too_narrow",
                        "message": f"签到窗口无法容纳 {start_node}→{end_node} 的耗时波动",
                    }
                )
                return []
            cursor = earliest_departure
            while cursor <= latest_departure:
                intervals.append((cursor, add_minutes(cursor, rmax)))
                cursor = add_minutes(cursor, 1)
        elif is_first_release_leg:
            # A worker may arrive as late as release_from + max_wait; the
            # robust end also covers the whole hospital release range.
            latest_ready = add_minutes(booking.release_from, booking.max_wait)
            ready = booking.release_from
            while ready <= latest_ready:
                worst_departure = max(ready, booking.release_until)
                end = add_minutes(worst_departure, rmax)
                if leg == "follow":
                    deadline = demand.household.deadline_for_date(booking.date)
                    if deadline and end > deadline:
                        ready = add_minutes(ready, 1)
                        continue
                intervals.append((ready, end))
                ready = add_minutes(ready, 1)
        else:
            if previous_end is None:
                # Defensive: later legs need the prior robust arrival.
                return []
            intervals.append((previous_end, add_minutes(previous_end, rmax)))

        eligible = [
            shift
            for shift in shifts
            if shift.date == booking.date
            and start_node in shift.locations
            and end_node in shift.locations
        ]
        if not eligible:
            reasons.append(
                {
                    "code": "no_shift_covers_locations",
                    "message": f"没有班组同时覆盖 {start_node} 和 {end_node}",
                }
            )
            return []

        candidates: list[list[Assignment]] = []
        seen_signatures: set[tuple[str, ...]] = set()
        for interval_start, interval_end in intervals:
            fitting = [
                shift
                for shift in eligible
                if shift.start <= interval_start and shift.end >= interval_end
            ]
            if len({s.worker for s in fitting}) < crew_count:
                reasons.append(
                    {
                        "code": "shift_window_or_crew_shortage",
                        "message": (
                            f"{leg} 需要 {crew_count} 人同时覆盖 "
                            f"{interval_start:%H:%M}–{interval_end:%H:%M}，"
                            f"符合时段的员工不足"
                        ),
                    }
                )
                continue

            # Prefer deterministic shift order.
            fitting = sorted(fitting, key=lambda s: (s.start, s.shift))
            for combo in combinations(fitting, crew_count):
                workers = [s.worker for s in combo]
                if len(set(workers)) != crew_count:
                    continue
                assignments = [
                    Assignment(
                        role=leg,
                        start_node=start_node,
                        end_node=end_node,
                        start=interval_start,
                        end=interval_end,
                        shift=shift.shift,
                        worker=shift.worker,
                        source=shift.source,
                        plan=shift.plan,
                    )
                    for shift in combo
                ]
                if not self._assignments_free(assignments, busy, reasons):
                    continue
                signature = tuple(
                    sorted(
                        (
                            a.worker,
                            a.start.isoformat(),
                            a.end.isoformat(),
                            a.role,
                        )
                        for a in assignments
                    )
                )
                if signature in seen_signatures:
                    continue
                seen_signatures.add(signature)
                candidates.append(assignments)
        return candidates

    def _assignments_free(
        self,
        assignments: list[Assignment],
        busy: list[BusyBlock],
        reasons: list[dict[str, str]] | None = None,
    ) -> bool:
        for assignment in assignments:
            for block in busy:
                if block.worker != assignment.worker:
                    continue
                if overlaps(
                    assignment.start,
                    assignment.end,
                    block.start,
                    block.end,
                ):
                    if reasons is not None:
                        reasons.append(
                            {
                                "code": "worker_promised_to_other_household",
                                "message": (
                                    f"{assignment.role} 候选员工 {assignment.worker} "
                                    "在承诺时段已有安排"
                                ),
                            }
                        )
                    return False

                # Adjacent intervals are allowed only when the worker can
                # robustly travel between the previous end node and next start.
                if assignment.end <= block.start:
                    if not self._transition_possible(
                        assignment.end_node,
                        block.start_node,
                        block.start - assignment.end,
                    ):
                        if reasons is not None:
                            reasons.append(
                                {
                                    "code": "worker_transition_not_verifiable",
                                    "message": (
                                        f"{assignment.worker} 从上一作业点 "
                                        f"{assignment.end_node} 到 {block.start_node} "
                                        "的间隔无法按路线上限兑现"
                                    ),
                                }
                            )
                        return False
                elif block.end <= assignment.start:
                    if not self._transition_possible(
                        block.end_node,
                        assignment.start_node,
                        assignment.start - block.end,
                    ):
                        if reasons is not None:
                            reasons.append(
                                {
                                    "code": "worker_transition_not_verifiable",
                                    "message": (
                                        f"{assignment.worker} 从承诺作业点 "
                                        f"{block.end_node} 到 {assignment.start_node} "
                                        "的间隔无法按路线上限兑现"
                                    ),
                                }
                            )
                        return False
        return True

    def _transition_possible(
        self, from_node: str | None, to_node: str | None, gap
    ) -> bool:
        gap_minutes = int(gap.total_seconds() // 60)
        if not from_node or not to_node:
            # Source commitments always have derived nodes; do not invent a
            # transition when an upstream record lacks location semantics.
            return True
        route = self.dataset.routes_on(from_node, to_node)
        if route is None:
            return from_node == to_node
        return gap_minutes >= route[1]
