"""Actual fulfillment and missing-proof status analysis."""

from __future__ import annotations

from typing import Any

from .models import Dataset
from .timeutil import parse_date


class OutcomeAnalyzer:
    def __init__(self, dataset: Dataset):
        self.dataset = dataset

    def actual_outcomes(self, as_of: str | None = None) -> dict[str, Any]:
        cutoff = None if as_of is None else self._parse_dt(as_of)
        booking_results: list[dict[str, Any]] = []
        checkpoint_counts: dict[str, dict[str, int]] = {}

        for booking in sorted(self.dataset.bookings, key=lambda b: b.booking):
            household = self.dataset.household(booking.home)
            notices = [
                n
                for n in self.dataset.notices
                if n.booking == booking.booking
                and (cutoff is None or n.noted_at <= cutoff)
            ]
            cancellation = next((n for n in notices if n.kind == "cancelled"), None)
            intake = [
                row
                for row in self.dataset.intake
                if row.booking == booking.booking
                and (cutoff is None or row.known_at <= cutoff)
            ]
            unaccepted = next(
                (row for row in intake if row.disposition == "no_complete_trip"), None
            )

            all_receipts = [
                r for r in self.dataset.receipts if r.booking == booking.booking
            ]
            receipts = [
                r
                for r in all_receipts
                if cutoff is None or r.reported_at <= cutoff
            ]
            trip_ids = sorted({r.trip for r in all_receipts} | {c.trip for c in self.dataset.commitments if c.booking == booking.booking})
            commitments_for_booking = [
                c for c in self.dataset.commitments if c.booking == booking.booking
            ]
            trip_id = trip_ids[0] if trip_ids else None

            checkpoints: list[dict[str, Any]] = []
            if cancellation:
                status = "cancelled"
                checkpoints.append(
                    {
                        "point_type": "booking",
                        "point": booking.clinic,
                        "status": "cancelled",
                        "occurred_at": cancellation.occurred_at.isoformat(timespec="minutes"),
                        "registered_at": cancellation.noted_at.isoformat(timespec="minutes"),
                        "source": "appointment_notices",
                    }
                )
            elif unaccepted and not trip_id:
                status = "not_accepted"
                checkpoints.append(
                    {
                        "point_type": "intake",
                        "point": booking.clinic,
                        "status": "not_accepted",
                        "registered_at": unaccepted.known_at.isoformat(timespec="minutes"),
                        "disposition": unaccepted.disposition,
                    }
                )
            else:
                status = "recorded"
                has_service_trace = bool(trip_id or receipts or commitments_for_booking)
                if not has_service_trace:
                    status = "no_actual_record"
                    checkpoints.append(
                        {
                            "point_type": "record",
                            "point": booking.clinic,
                            "status": "no_actual_record",
                            "rule": "无受理结论、承诺或凭据；日档不完整时不推断为已接单或缺证待核",
                        }
                    )
                else:
                    checkpoints.extend(
                        self._hospital_checkpoint(booking.booking, receipts, notices, booking)
                    )
                    checkpoints.extend(self._release_checkpoint(receipts, booking))
                    checkpoints.extend(
                        self._handover_checkpoints(
                            booking.booking,
                            trip_id,
                            household,
                            booking,
                            receipts,
                            cutoff,
                        )
                    )

            for checkpoint in checkpoints:
                point_type = checkpoint["point_type"]
                checkpoint_counts.setdefault(point_type, {})
                checkpoint_counts[point_type][checkpoint["status"]] = (
                    checkpoint_counts[point_type].get(checkpoint["status"], 0) + 1
                )

            release = next((r for r in receipts if r.kind == "released"), None)
            arrival = next((r for r in receipts if r.kind == "arrived"), None)
            if arrival is None:
                arrived_notice = next((n for n in notices if n.kind == "arrived"), None)
                arrival_time = arrived_notice.occurred_at if arrived_notice else None
            else:
                arrival_time = arrival.happened_at
            hospital_elapsed = None
            if release and arrival_time:
                hospital_elapsed = int(
                    (release.happened_at - arrival_time).total_seconds() // 60
                )

            booking_results.append(
                {
                    "booking": booking.booking,
                    "home": booking.home,
                    "date": booking.date.isoformat(),
                    "trip": trip_id,
                    "overall_status": status,
                    "intake_disposition": intake[-1].disposition if intake else None,
                    "hospital_elapsed_minutes_if_both_evidence": hospital_elapsed,
                    "receipts": [self._receipt_dict(r) for r in all_receipts],
                    "wait_metrics_note": (
                        "仅有完整到离院凭据时计算院内停留；缺凭据为 null，不当作 0 等待"
                    ),
                    "checkpoints": checkpoints,
                    "evidence_visibility": {
                        "as_of": cutoff.isoformat(timespec="minutes") if cutoff else "all_records",
                        "visible_receipts": [r.receipt for r in receipts],
                        "later_reported_receipts": [
                            r.receipt
                            for r in all_receipts
                            if cutoff is not None and r.reported_at > cutoff
                        ],
                    },
                }
            )

        hospital_elapsed_by_site = self._hospital_elapsed_by_site(booking_results)

        return {
            "as_of": cutoff.isoformat(timespec="minutes") if cutoff else None,
            "checkpoint_counts": checkpoint_counts,
            "hospital_elapsed_by_site": hospital_elapsed_by_site,
            "hospital_average_usage_rule": (
                "仅作实际结果展示；加班重放不使用院内平均耗时排班，而使用完整释放窗口和路线上限"
            ),
            "bookings": booking_results,
        }

    @staticmethod
    def _hospital_elapsed_by_site(booking_results: list[dict[str, Any]]) -> dict[str, Any]:
        values: dict[str, list[int]] = {}
        for booking in booking_results:
            elapsed = booking.get("hospital_elapsed_minutes_if_both_evidence")
            if elapsed is None:
                continue
            hospital = next(
                (
                    c.get("point")
                    for c in booking["checkpoints"]
                    if c.get("point_type") == "hospital"
                ),
                None,
            )
            if hospital:
                values.setdefault(hospital, []).append(elapsed)
        return {
            site: {
                "sample_count": len(samples),
                "elapsed_minutes": samples,
                "average_elapsed_minutes": sum(samples) / len(samples),
                "missing_evidence_rule": "缺少到/离院凭据的记录不参与平均，不按 0 分钟计入",
            }
            for site, samples in sorted(values.items())
        }

    @staticmethod
    def _receipt_dict(receipt) -> dict[str, Any]:
        return {
            "receipt": receipt.receipt,
            "trip": receipt.trip,
            "booking": receipt.booking,
            "kind": receipt.kind,
            "location": receipt.location,
            "actual_at": receipt.happened_at.isoformat(timespec="minutes"),
            "registered_at": receipt.reported_at.isoformat(timespec="minutes"),
            "proof": receipt.proof,
        }

    def _release_checkpoint(self, receipts, booking) -> list[dict[str, Any]]:
        released = next((r for r in receipts if r.kind == "released"), None)
        if not released:
            return [
                {
                    "point_type": "hospital_release",
                    "point": booking.clinic,
                    "status": "missing_evidence",
                    "expected_window": [
                        booking.release_from.isoformat(timespec="minutes"),
                        booking.release_until.isoformat(timespec="minutes"),
                    ],
                }
            ]
        if released.happened_at < booking.release_from:
            status = "early_outside_window"
        elif released.happened_at <= booking.release_until:
            status = "on_time"
        else:
            status = "late"
        return [
            {
                "point_type": "hospital_release",
                "point": booking.clinic,
                "status": status,
                "actual_at": released.happened_at.isoformat(timespec="minutes"),
                "registered_at": released.reported_at.isoformat(timespec="minutes"),
                "late_minutes": max(
                    0,
                    int((released.happened_at - booking.release_until).total_seconds() // 60),
                ),
                "source": "trip_receipts",
                "proof": released.proof,
            }
        ]

    def _hospital_checkpoint(self, booking_id, receipts, notices, booking) -> list[dict[str, Any]]:
        arrived = next((r for r in receipts if r.kind == "arrived"), None)
        arrived_notice = next((n for n in notices if n.kind == "arrived"), None)
        if arrived:
            actual = arrived.happened_at
            registered = arrived.reported_at
            source = "trip_receipts"
            proof = arrived.proof
        elif arrived_notice:
            actual = arrived_notice.occurred_at
            registered = arrived_notice.noted_at
            source = "appointment_notices"
            proof = None
        else:
            return [
                {
                    "point_type": "hospital",
                    "point": booking.clinic,
                    "status": "missing_evidence",
                    "expected_window": [
                        booking.arrival_from.isoformat(timespec="minutes"),
                        booking.arrival_until.isoformat(timespec="minutes"),
                    ],
                }
            ]

        if actual < booking.arrival_from:
            status = "early_outside_window"
        elif actual <= booking.arrival_until:
            status = "on_time"
        else:
            status = "late"
        late_minutes = max(0, int((actual - booking.arrival_until).total_seconds() // 60))
        return [
            {
                "point_type": "hospital",
                "point": booking.clinic,
                "status": status,
                "actual_at": actual.isoformat(timespec="minutes"),
                "registered_at": registered.isoformat(timespec="minutes"),
                "late_minutes": late_minutes,
                "source": source,
                "proof": proof,
            }
        ]

    def _handover_checkpoints(
        self, booking_id, trip_id, household, booking, receipts, as_of
    ) -> list[dict[str, Any]]:
        expected: list[tuple[str, Any, str | None]] = []
        if household.following_site:
            expected.append(
                (
                    household.following_site,
                    household.deadline_for_date(booking.date),
                    "follow",
                )
            )
        expected.append((household.return_to, self._return_deadline(trip_id), "return"))

        results: list[dict[str, Any]] = []
        handovers = [r for r in receipts if r.kind == "handed_over"]
        for location, deadline, role in expected:
            matching = [r for r in handovers if r.location == location and r.proof]
            if not matching:
                gap_notice = next(
                    (
                        n
                        for n in self.dataset.notices
                        if n.booking == booking_id and n.kind == "handover_no_signature"
                    ),
                    None,
                )
                results.append(
                    {
                        "point_type": "handover",
                        "point": location,
                        "role": role,
                        "status": "missing_proof_pending",
                        "deadline": deadline.isoformat(timespec="minutes") if deadline else None,
                        "gap_notice_at": gap_notice.noted_at.isoformat(timespec="minutes")
                        if gap_notice
                        else None,
                        "rule": "缺末段交接凭据时保持待核，不重新派去程；不得按零等待处理",
                    }
                )
                continue

            receipt = sorted(matching, key=lambda r: r.reported_at)[-1]
            status = "on_time"
            late_minutes = 0
            if deadline and receipt.happened_at > deadline:
                status = "late"
                late_minutes = int((receipt.happened_at - deadline).total_seconds() // 60)
            registration_lag = int(
                (receipt.reported_at - receipt.happened_at).total_seconds() // 60
            )
            backfilled = receipt.reported_at.date() > receipt.happened_at.date()
            results.append(
                {
                    "point_type": "handover",
                    "point": location,
                    "role": role,
                    "status": status,
                    "registration_status": "late_backfill" if backfilled else "registered",
                    "actual_at": receipt.happened_at.isoformat(timespec="minutes"),
                    "registered_at": receipt.reported_at.isoformat(timespec="minutes"),
                    "deadline": deadline.isoformat(timespec="minutes") if deadline else None,
                    "late_minutes": late_minutes,
                    "registration_lag_minutes": registration_lag,
                    "backfilled": backfilled,
                    "backfilled_after_archive": as_of is not None
                    and receipt.reported_at > as_of,
                    "receipt": receipt.receipt,
                    "proof": receipt.proof,
                }
            )
        return results

    def _return_deadline(self, trip_id: str | None):
        if not trip_id:
            return None
        returns = [
            c
            for c in self.dataset.commitments
            if c.trip == trip_id and c.role == "return"
        ]
        return returns[0].end if returns else None

    @staticmethod
    def _parse_dt(value: str):
        from .timeutil import parse_dt

        return parse_dt(value)
