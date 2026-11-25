"""Project facade: confirm trips, replay quotes, analyze results, save runs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .engine import CareDesk
from .io import load_dataset
from .outcomes import OutcomeAnalyzer
from .timeutil import parse_dt

TRIP_KIND_LABELS = {
    "outbound_return_home": "单程去院后直接返程回家（双人/单人按预约）",
    "outbound_follow_return_home": "出门→医院→后续地点交接→回家的完整行程",
    "outbound_follow_handover": "出门→医院→后续地点并完成交接",
    "outbound_then_authorized_relay": "去程后由已批准授权亲属在医院接棒，替代返程",
}


class CareProject:
    def __init__(self, data_dir: str | Path = "data", store_dir: str | Path = "run_history"):
        self.data_dir = Path(data_dir)
        self.store_dir = Path(store_dir)
        self.dataset = load_dataset(self.data_dir)
        self.desk = CareDesk(self.dataset)
        self.outcomes = OutcomeAnalyzer(self.dataset)

    # ------------------------------------------------------------------
    # Coordination / confirmation
    # ------------------------------------------------------------------
    def confirmation_plan(
        self,
        day: str = "2026-06-10",
        as_of: str | datetime = "2026-06-08T13:05",
        quote_plans: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        moment = parse_dt(as_of) if isinstance(as_of, str) else as_of
        plan = self.desk.plan_day(day, as_of=moment, quote_plans=quote_plans)
        plan["quote_plans"] = sorted(set(quote_plans or []))
        plan["trip_kind_labels"] = TRIP_KIND_LABELS
        return plan

    # ------------------------------------------------------------------
    # Historical replay and quote comparison
    # ------------------------------------------------------------------
    def archive_evidence_status(self, day: str) -> dict[str, Any]:
        closing = self.dataset.closing_day(day)
        bookings = [b for b in self.dataset.bookings if b.date == closing.day]
        intake = [
            row
            for row in self.dataset.intake
            if row.known_at <= closing.cutoff
            and any(b.booking == row.booking for b in bookings)
        ]
        intake_bookings = {row.booking for row in intake}
        shifts = [s for s in self.dataset.shifts if s.date == closing.day]
        missing_intake = sorted(b.booking for b in bookings if b.booking not in intake_bookings)
        supply_complete = len(shifts) > 0
        evidence_complete = closing.complete and not missing_intake and supply_complete
        return {
            "day": closing.day.isoformat(),
            "declared_complete": closing.complete,
            "booking_count": len(bookings),
            "intake_booking_count": len(intake_bookings),
            "missing_intake_at_cutoff": missing_intake,
            "shift_count": len(shifts),
            "supply_evidence_present": supply_complete,
            "usable_for_increment": evidence_complete,
        }

    def comparable_days(self, quote_plan: str | None = None) -> list[dict[str, Any]]:
        quote = self._quote(quote_plan) if quote_plan else None
        rows: list[dict[str, Any]] = []
        for closing in sorted(self.dataset.closing_days, key=lambda c: c.day):
            evidence = self.archive_evidence_status(closing.day.isoformat())
            if not evidence["usable_for_increment"]:
                continue
            if quote:
                if closing.unit != quote.unit:
                    continue
                if closing.day.isoweekday() != quote.weekday:
                    continue
            rows.append(
                {
                    "day": closing.day.isoformat(),
                    "weekday": closing.day.isoweekday(),
                    "unit": closing.unit,
                    "cutoff": closing.cutoff.isoformat(timespec="minutes"),
                    "recorded_at": closing.recorded_at.isoformat(timespec="minutes"),
                    "complete": closing.complete,
                    "evidence": evidence,
                }
            )
        return rows

    def substitution_audit(self, quote) -> list[dict[str, Any]]:
        """List old promised roles a quote worker could cover.

        This is intentionally only an audit. Replay keeps old commitments fixed,
        so any such substitution is never counted as a new household.
        """
        result: list[dict[str, Any]] = []
        days = self.comparable_days(quote.plan)
        for day_info in days:
            from .timeutil import parse_date

            day = parse_date(day_info["day"])
            quote_shifts = quote.shifts_for(day)
            for commitment in self.dataset.commitments:
                if commitment.start.date() != day or commitment.status != "confirmed":
                    continue
                start_node, end_node = self.desk._commitment_nodes(commitment)
                coverable_by = []
                for shift in quote_shifts:
                    if (
                        shift.start <= commitment.start
                        and shift.end >= commitment.end
                        and start_node
                        and end_node
                        and start_node in shift.locations
                        and end_node in shift.locations
                    ):
                        coverable_by.append(shift.shift)
                if coverable_by:
                    result.append(
                        {
                            "day": day_info["day"],
                            "trip": commitment.trip,
                            "booking": commitment.booking,
                            "role": commitment.role,
                            "old_shift": commitment.shift,
                            "quote_shifts_able_to_replace": coverable_by,
                            "net_new_treatment": "替换原承诺不计增单；固定承诺 replay 不移动",
                        }
                    )
        return result

    def replay_day(
        self,
        day: str,
        quote_plan: str | None = None,
        *,
        as_of: str | datetime | None = None,
    ) -> dict[str, Any]:
        closing = self.dataset.closing_day(day)
        cutoff = closing.cutoff if as_of is None else (
            parse_dt(as_of) if isinstance(as_of, str) else as_of
        )
        plans = [quote_plan] if quote_plan else None
        baseline = self.desk.plan_day(day, as_of=cutoff)
        quoted = self.desk.plan_day(day, as_of=cutoff, quote_plans=plans)

        baseline_ok = {
            row["demand_key"]: row for row in baseline["results"] if row["ok"]
        }
        quoted_ok = {row["demand_key"]: row for row in quoted["results"] if row["ok"]}
        added_keys = sorted(set(quoted_ok) - set(baseline_ok))

        demand_records = self.desk.demands_for_day(day, as_of=cutoff)
        included_bookings = sorted(
            booking.booking
            for demand in demand_records
            for booking in demand.options
        )
        original_shifts = [
            shift.shift
            for shift in self.dataset.shifts
            if shift.date == closing.day
        ]
        fixed_commitments = sorted(
            {
                c.trip
                for c in self.dataset.commitments
                if c.start.date() == closing.day
            }
        )

        added: list[dict[str, Any]] = []
        for key in added_keys:
            row = quoted_ok[key]
            added.append(
                {
                    "demand_key": key,
                    "booking": row.get("selected_booking"),
                    "trip_kind": row.get("trip_kind") or "already_confirmed",
                    "trip_kind_label": TRIP_KIND_LABELS.get(
                        row.get("trip_kind") or "", row.get("trip_kind")
                    ),
                    "assignments": row.get("assignments", []),
                }
            )

        remaining = [
            {
                "demand_key": row["demand_key"],
                "booking": row.get("selected_booking"),
                "reasons": row.get("reasons", []),
            }
            for row in quoted["results"]
            if not row["ok"]
        ]

        return {
            "day": closing.day.isoformat(),
            "weekday": closing.day.isoweekday(),
            "unit": closing.unit,
            "cutoff": cutoff.isoformat(timespec="minutes"),
            "complete_archive": closing.complete,
            "quote_plan": quote_plan,
            "conditions": {
                "included_bookings_at_cutoff": included_bookings,
                "original_shift_ids": original_shifts,
                "protected_commitment_trips": fixed_commitments,
                "visibility_rule": (
                    "只使用 cutoff 前 known/noted/received 的请求、受理结论、"
                    "授权和承诺；实际离院、事后批准、迟来签收和 cutoff 后取消不参与排班"
                ),
                "source_sha256": self.source_hashes(),
            },
            "baseline_served_keys": sorted(baseline_ok),
            "quote_served_keys": sorted(quoted_ok),
            "additional_households": added,
            "additional_count": len(added),
            "still_unaccepted": remaining,
            "baseline_plan": baseline,
            "quote_plan_result": quoted,
        }

    def compare_quotes(
        self, *, decision_at: str | datetime | None = None
    ) -> dict[str, Any]:
        decision_moment = (
            parse_dt(decision_at) if isinstance(decision_at, str) else decision_at
        )
        comparisons: list[dict[str, Any]] = []
        all_replay_days: set[str] = set()

        for quote in sorted(self.dataset.quotes, key=lambda q: q.plan):
            days = self.comparable_days(quote.plan)
            all_replay_days.update(day["day"] for day in days)
            replays = [self.replay_day(day["day"], quote.plan) for day in days]
            added_total = sum(item["additional_count"] for item in replays)
            added_households = [
                entry for replay in replays for entry in replay["additional_households"]
            ]
            remaining = [
                {"day": replay["day"], **item}
                for replay in replays
                for item in replay["still_unaccepted"]
            ]
            substitutions = self.substitution_audit(quote)
            replay_cost = quote.cost_cents * len(days)
            valid_now = True
            if decision_moment is not None:
                valid_now = decision_moment <= quote.valid_until

            category_counts: dict[str, int] = {}
            for item in added_households:
                kind = item["trip_kind"]
                category_counts[kind] = category_counts.get(kind, 0) + 1

            comparisons.append(
                {
                    "plan": quote.plan,
                    "unit": quote.unit,
                    "weekday": quote.weekday,
                    "window": f"{quote.start_hm}-{quote.end_hm}",
                    "locations": sorted(quote.locations),
                    "shift_count_per_day": quote.shift_count,
                    "cost_cents_per_applicable_day": quote.cost_cents,
                    "cost_yuan_per_applicable_day": quote.cost_cents / 100,
                    "valid_until": quote.valid_until.isoformat(timespec="minutes"),
                    "quote_accepted_by_decision_time": valid_now,
                    "replayed_days": [day["day"] for day in days],
                    "replay_day_details": replays,
                    "additional_household_day_trips": added_total,
                    "distinct_added_homes": sorted(
                        {item["demand_key"].split("@", 1)[0] for item in added_households}
                    ),
                    "average_additional_per_applicable_day": (
                        added_total / len(days) if days else 0
                    ),
                    "added_category_counts": category_counts,
                    "added_category_labels": {
                        key: TRIP_KIND_LABELS.get(key, key) for key in category_counts
                    },
                    "replay_cost_cents_for_all_comparable_days": replay_cost,
                    "replay_cost_yuan_for_all_comparable_days": replay_cost / 100,
                    "cost_cents_per_added_household_in_replay": (
                        replay_cost // added_total if added_total else None
                    ),
                    "remaining_unaccepted": remaining,
                    "substitution_audit": substitutions,
                    "net_new_only_rule": (
                        "已确认 Y1/Y2 等行程作为硬承诺保留；新增只计原 baseline 未覆盖户，"
                        "不把 Quote 班组替换原有订单算作增单"
                    ),
                }
            )

        usable_days = self.comparable_days()
        quote_matching_days = [
            day
            for day in usable_days
            if any(
                quote.unit == day["unit"]
                and quote.weekday == day["weekday"]
                for quote in self.dataset.quotes
            )
        ]
        recommended = self._recommendation(comparisons, quote_matching_days, decision_moment)
        return {
            "comparable_complete_days": quote_matching_days,
            "all_evidence_complete_days": usable_days,
            "excluded_days": [
                {
                    "day": closing.day.isoformat(),
                    "weekday": closing.day.isoweekday(),
                    "complete": closing.complete,
                    "unit": closing.unit,
                    "reason": "日档不完整，不能作为增班收益依据"
                    if not closing.complete
                    else None,
                }
                for closing in sorted(self.dataset.closing_days, key=lambda c: c.day)
                if not closing.complete
            ],
            "comparisons": comparisons,
            "recommendation": recommended,
            "actual_outcomes_summary": self.actual_outcomes(),
        }

    @staticmethod
    def _recommendation(
        comparisons: list[dict[str, Any]],
        usable_days: list[dict[str, Any]],
        decision_moment: datetime | None,
    ) -> dict[str, Any]:
        if not usable_days or not any(c.get("replayed_days") for c in comparisons):
            return {
                "code": "no_comparable_complete_day",
                "text": "没有同单元、同星期且受理/供给/登记完整的适用工作日，不推荐加班。",
                "recommended_plans": [],
            }
        effective = [c for c in comparisons if c["additional_household_day_trips"] > 0]
        if decision_moment is not None:
            accepting = [c for c in effective if c["quote_accepted_by_decision_time"]]
            if not accepting:
                if effective:
                    return {
                        "code": "historical_gain_but_quotes_expired",
                        "text": "历史重放显示过增量，但这些报价已过 valid_until；请取新工作周报价后再比较。",
                        "recommended_plans": [],
                        "historically_effective_plans": [c["plan"] for c in effective],
                    }
                return {
                    "code": "no_increment",
                    "text": "所有未过期报价都不能在保护已确认承诺后增加完整行程，不推荐为增量接单加班。",
                    "recommended_plans": [],
                }
            effective = accepting
        if not effective:
            return {
                "code": "no_increment",
                "text": "没有报价能在保护已确认承诺后增加完整行程，不推荐加班。",
                "recommended_plans": [],
            }
        best_cost = min(
            c["cost_cents_per_added_household_in_replay"]
            for c in effective
            if c["cost_cents_per_added_household_in_replay"] is not None
        )
        best = [
            c["plan"]
            for c in effective
            if c["cost_cents_per_added_household_in_replay"] == best_cost
        ]
        return {
            "code": "recommend_net_new_complete_trips",
            "text": (
                "仅有能补齐双人末段能力的报价产生净新增完整行程；"
                "E2 类只补一人的报价不能在不破坏已确认承诺的情况下接下 team 户。"
            ),
            "recommended_plans": best,
            "cost_cents_per_added_household_in_replay": best_cost,
        }

    def actual_outcomes(self, as_of: str | None = None) -> dict[str, Any]:
        return self.outcomes.actual_outcomes(as_of)

    # ------------------------------------------------------------------
    # Persistent comparison / audit trail
    # ------------------------------------------------------------------
    def source_hashes(self) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for path in sorted(self.data_dir.glob("*.csv")):
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes

    def save_json(self, payload: Any, filename: str, out_dir: str | Path = "reports") -> Path:
        directory = Path(out_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / filename
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False),
            encoding="utf-8",
        )
        return path

    def save_run_snapshot(
        self,
        payload: dict[str, Any],
        *,
        label: str,
        out_dir: str | Path | None = None,
    ) -> Path:
        directory = Path(out_dir or self.store_dir)
        directory.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + f"-{label}"
        path = directory / f"{run_id}.json"
        snapshot = {
            "run_id": run_id,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "label": label,
            "data_dir": str(self.data_dir),
            "conditions": payload.get("comparable_complete_days")
            or payload.get("as_of")
            or {},
            "source_hashes": self.source_hashes(),
            "payload": payload,
        }
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        index_path = directory / "index.jsonl"
        with index_path.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "run_id": run_id,
                        "label": label,
                        "path": str(path),
                        "saved_at": snapshot["saved_at"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return path

    def list_run_snapshots(self, directory: str | Path | None = None) -> list[dict[str, Any]]:
        store = Path(directory or self.store_dir)
        index = store / "index.jsonl"
        if not index.exists():
            return []
        rows = []
        for line in index.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def get_quote(self, plan: str) -> Quote:
        return self._quote(plan)

    def _quote(self, plan: str | None):
        if plan is None:
            return None
        for quote in self.dataset.quotes:
            if quote.plan == plan:
                return quote
        raise KeyError(plan)
