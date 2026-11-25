import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from care_desk import CareProject  # noqa: E402
from care_desk.timeutil import parse_dt  # noqa: E402


class QuoteReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = CareProject(ROOT / "data")
        cls.report = cls.project.compare_quotes()

    def test_only_complete_matching_weekdays_are_replayed(self):
        days = [row["day"] for row in self.report["comparable_complete_days"]]
        self.assertEqual(days, ["2026-05-27", "2026-06-03"])
        excluded = {row["day"]: row for row in self.report["excluded_days"]}
        self.assertFalse(excluded["2026-06-04"]["complete"])
        self.assertFalse(excluded["2026-06-10"]["complete"])

    def test_only_two_worker_late_quote_adds_complete_trips(self):
        by_plan = {row["plan"]: row for row in self.report["comparisons"]}

        self.assertEqual(by_plan["E1"]["additional_household_day_trips"], 0)
        self.assertEqual(by_plan["E2"]["additional_household_day_trips"], 0)
        self.assertEqual(by_plan["E3"]["additional_household_day_trips"], 2)
        self.assertEqual(by_plan["E3"]["distinct_added_homes"], ["H5", "H7"])
        self.assertEqual(
            by_plan["E3"]["added_category_counts"],
            {"outbound_return_home": 2},
        )
        self.assertEqual(
            by_plan["E3"]["replay_cost_cents_for_all_comparable_days"], 60_000
        )
        self.assertEqual(
            by_plan["E3"]["cost_cents_per_added_household_in_replay"], 30_000
        )
        self.assertEqual(self.report["recommendation"]["recommended_plans"], ["E3"])

    def test_e2_failure_is_team_crew_shortage_around_protected_trip(self):
        replay = self.project.replay_day("2026-05-27", "E2")
        self.assertEqual(replay["additional_count"], 0)
        remaining = replay["still_unaccepted"][0]
        codes = {
            reason["code"]
            for reason in remaining["reasons"]
        }
        self.assertIn("worker_promised_to_other_household", codes)

    def test_replay_records_cutoff_conditions_and_source_hashes(self):
        replay = self.project.replay_day("2026-06-03", "E3")
        conditions = replay["conditions"]
        self.assertEqual(replay["cutoff"], "2026-06-02T16:00")
        self.assertEqual(conditions["included_bookings_at_cutoff"], ["P3", "P4"])
        self.assertEqual(conditions["original_shift_ids"], ["S21", "S22", "S24", "S23"])
        self.assertEqual(conditions["protected_commitment_trips"], ["Y2"])
        self.assertIn("bookings.csv", conditions["source_sha256"])
        self.assertEqual(len(conditions["source_sha256"]["bookings.csv"]), 64)

    def test_decision_after_quote_expiry_does_not_recommend_old_quote(self):
        report = self.project.compare_quotes(decision_at="2026-11-25T09:00")
        self.assertEqual(report["recommendation"]["code"], "historical_gain_but_quotes_expired")
        self.assertEqual(report["recommendation"]["recommended_plans"], [])


class ConfirmationAndEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = CareProject(ROOT / "data")

    def test_alternative_bookings_are_one_household_demand(self):
        demands = self.project.desk.demands_for_day(
            "2026-06-10", as_of=parse_dt("2026-06-08T13:05")
        )
        h1 = next(demand for demand in demands if demand.home == "H1")
        self.assertEqual([b.booking for b in h1.options], ["A2"])
        self.assertEqual(h1.key, "H1@2026-06-10")

    def test_approved_location_and_validity_release_return_resource(self):
        plan = self.project.confirmation_plan()
        h2 = next(row for row in plan["results"] if row["home"] == "H2")
        self.assertEqual(h2["status"], "reserved_complete")
        self.assertEqual(h2["selected_booking"], "A3")
        self.assertEqual(h2["trip_kind"], "outbound_then_authorized_relay")
        self.assertEqual(h2["relay"]["authorization"], "AUTH1")
        self.assertEqual(len(h2["assignments"]), 1)

    def test_only_approved_visible_authorization_releases_return(self):
        before = self.project.desk.plan_day(
            "2026-06-10", as_of=parse_dt("2026-06-08T12:59")
        )
        h2_before = next(row for row in before["results"] if row["home"] == "H2")
        self.assertEqual(h2_before["trip_kind"], "outbound_return_home")
        self.assertIsNone(h2_before["relay"])
        self.assertEqual(len(h2_before["assignments"]), 2)

        after = self.project.confirmation_plan()
        h2_after = next(row for row in after["results"] if row["home"] == "H2")
        self.assertEqual(h2_after["trip_kind"], "outbound_then_authorized_relay")

    def test_priority_and_protected_a4_leave_h1_without_atomic_plan(self):
        plan = self.project.confirmation_plan()
        homes = [row["home"] for row in plan["results"]]
        self.assertEqual(homes, ["H2", "H1", "H3"])
        h1 = next(row for row in plan["results"] if row["home"] == "H1")
        h3 = next(row for row in plan["results"] if row["home"] == "H3")
        self.assertFalse(h1["ok"])
        self.assertEqual(h3["status"], "already_confirmed")
        self.assertEqual(h3["already_confirmed_trip"], "Y3")

    def test_early_quote_does_not_solve_late_team_bottleneck(self):
        plan = self.project.confirmation_plan(quote_plans=["E1"])
        h1 = next(row for row in plan["results"] if row["home"] == "H1")
        self.assertFalse(h1["ok"])

    def test_cancellation_scope_is_limited_and_remaining_promise_protected(self):
        scopes = self.project.desk.cancellation_scopes(
            parse_dt("2026-06-08T13:05")
        )
        a1 = next(scope for scope in scopes if scope["booking"] == "A1")
        self.assertEqual(a1["invalid_scope"], ["booking_option:A1"])
        self.assertTrue(a1["whole_rebooking_consent_required"])

    def test_late_hospital_and_handover_statuses_are_separate(self):
        outcomes = self.project.actual_outcomes()
        p3 = next(row for row in outcomes["bookings"] if row["booking"] == "P3")
        hospital = next(c for c in p3["checkpoints"] if c["point_type"] == "hospital")
        handover = next(c for c in p3["checkpoints"] if c["point_type"] == "handover")
        self.assertEqual(hospital["status"], "late")
        self.assertEqual(hospital["late_minutes"], 7)
        self.assertEqual(handover["status"], "on_time")

    def test_late_signature_completes_but_keeps_both_timestamps(self):
        archive = self.project.actual_outcomes("2026-06-11T12:00")
        a4_archive = next(row for row in archive["bookings"] if row["booking"] == "A4")
        pending = next(
            c for c in a4_archive["checkpoints"] if c["point_type"] == "handover"
        )
        self.assertEqual(pending["status"], "missing_proof_pending")

        current = self.project.actual_outcomes()
        a4 = next(row for row in current["bookings"] if row["booking"] == "A4")
        handover = next(c for c in a4["checkpoints"] if c["point_type"] == "handover")
        self.assertEqual(handover["status"], "on_time")
        self.assertEqual(handover["actual_at"], "2026-06-10T10:40")
        self.assertEqual(handover["registered_at"], "2026-06-12T12:00")
        self.assertGreater(handover["registration_lag_minutes"], 24 * 60)

    def test_missing_proof_is_not_converted_to_zero_wait(self):
        outcomes = self.project.actual_outcomes("2026-06-11T12:00")
        a4 = next(row for row in outcomes["bookings"] if row["booking"] == "A4")
        # A4 has arrival notice + release, so elapsed is known; unknown fields
        # elsewhere remain None rather than being filled with zero.
        self.assertEqual(a4["hospital_elapsed_minutes_if_both_evidence"], 63)
        for checkpoint in a4["checkpoints"]:
            if checkpoint["status"] == "missing_proof_pending":
                self.assertNotIn("wait_minutes", checkpoint)


class EmptyComparisonTests(unittest.TestCase):
    def test_no_comparable_day_means_no_overtime_recommendation(self):
        recommendation = CareProject._recommendation([], [], parse_dt("2026-06-15T09:00"))
        self.assertEqual(recommendation["code"], "no_comparable_complete_day")
        self.assertEqual(recommendation["recommended_plans"], [])


if __name__ == "__main__":
    unittest.main()
