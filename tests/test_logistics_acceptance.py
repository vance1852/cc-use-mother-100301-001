import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
import tempfile

from polar_station_foundation.errors import ConflictError
from polar_station_foundation.service import DomainService

from logistics_fixtures import MutableClock, build_world, freeze_and_decide, new_world
from polar_station_logistics.acceptance import run
from polar_station_logistics.service import LogisticsService
from polar_station_logistics.storage import open_database


class OfflineAcceptanceTest(unittest.TestCase):
    """离线端到端验收脚本的关键断言。"""

    def test_acceptance_story(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertEqual({
            "D-FOOD": "approved",
            "D-O2": "approved",
            "D-C1": "rejected",
            "D-C2": "approved",
            "D-F1": "approved",
            "D-F2": "partially_approved",
            "D-F3": "rejected",
            "D-F4": "rejected",
        }, result["decision_status"])
        self.assertEqual("institution_quota_exceeded", result["split_reject_reason"])
        self.assertTrue(result["conservation_after_decision"])
        self.assertTrue(result["critical_minimums_met"])
        self.assertEqual("approved", result["emergency_status"])
        self.assertEqual(1, result["emergency_pending_approvals"])
        self.assertTrue(result["emergency_first_approval_replayed"])
        self.assertTrue(result["confirm_replayed"])
        self.assertTrue(result["confirm_conflict_raised"])
        self.assertTrue(result["reallocation_conservation"])
        self.assertEqual("partially_approved", result["f1_latest_status"])
        self.assertEqual(250, result["f1_latest_approved"])
        self.assertEqual(4, result["restart_pending_count"])
        self.assertEqual("closed", result["final_flight_status"])
        self.assertTrue(result["conservation_final"])
        self.assertTrue(result["audit_valid"])


class RestartResumeTest(unittest.TestCase):
    """系统重启后接续未完成的装载确认。"""

    def test_restart_resumes_pending_confirmations(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
            database = open_database(path)
            service, foundation = build_world(database, clock)
            service.submit_declaration(request_id="rs-food", actor_id="op-001", declaration_id="D-RS1",
                                       flight_id="flight-001", item_id="food-pack", quantity=60, priority=5)
            service.submit_declaration(request_id="rs-o2", actor_id="op-001", declaration_id="D-RS2",
                                       flight_id="flight-001", item_id="o2-bottle", quantity=25, priority=5)
            freeze_and_decide(service, clock)
            pending_before = service.pending_load_confirmations("flight-001")
            self.assertEqual(2, pending_before["pending_count"])
            first = pending_before["pending"][0]
            service.confirm_load(request_id="rs-confirm-1", actor_id="op-001",
                                 allocation_id=first["allocation_id"],
                                 expected_version=first["version"])
            database.close()

            # 模拟系统重启：同一数据库文件重新打开
            database = open_database(path)
            service = LogisticsService(database, clock)
            foundation = DomainService(database, clock)
            pending_after = service.pending_load_confirmations("flight-001")
            self.assertEqual(1, pending_after["pending_count"])
            self.assertNotEqual(first["allocation_id"], pending_after["pending"][0]["allocation_id"])
            remaining = pending_after["pending"][0]
            service.confirm_load(request_id="rs-confirm-2", actor_id="op-001",
                                 allocation_id=remaining["allocation_id"],
                                 expected_version=remaining["version"])
            service.seal_flight(request_id="rs-seal", actor_id="admin-001", flight_id="flight-001")
            self.assertEqual("closed", service.get_flight("flight-001")["flight"]["status"])
            valid, _ = foundation.verify_audit()
            self.assertTrue(valid)
            database.close()


class ConcurrencyTest(unittest.TestCase):
    """并发确认同一份额时只有一方成功，不会重复扣减。"""

    def test_concurrent_confirm_only_one_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.sqlite3"
            clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
            database = open_database(path)
            service, foundation = build_world(database, clock)
            service.submit_declaration(request_id="cc-food", actor_id="op-001", declaration_id="D-CC",
                                       flight_id="flight-001", item_id="food-pack", quantity=60, priority=5)
            freeze_and_decide(service, clock)
            allocation = service.pending_load_confirmations("flight-001")["pending"][0]
            allocation_id = allocation["allocation_id"]

            other_database = open_database(path)
            other_service = LogisticsService(other_database, clock)
            barrier = threading.Barrier(2)
            outcomes = []

            def confirm(svc, request_id):
                barrier.wait(timeout=10)
                try:
                    svc.confirm_load(request_id=request_id, actor_id="op-001",
                                     allocation_id=allocation_id, expected_version=1)
                    outcomes.append("ok")
                except ConflictError:
                    outcomes.append("conflict")

            threads = [threading.Thread(target=confirm, args=(service, "cc-race-1")),
                       threading.Thread(target=confirm, args=(other_service, "cc-race-2"))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertEqual(sorted(outcomes), ["conflict", "ok"])
            row = database.connection.execute(
                "SELECT status, version FROM log_allocations WHERE allocation_id=?",
                (allocation_id,)).fetchone()
            self.assertEqual("loaded", row["status"])
            self.assertEqual(2, row["version"])
            self.assertTrue(service.conservation_report("flight-001")["all_ok"])
            other_database.close()
            database.close()

    def test_replay_same_request_never_double_deducts(self):
        service, foundation, clock, database = new_world()
        self.addCleanup(database.close)
        service.submit_declaration(request_id="dd-food", actor_id="op-001", declaration_id="D-DD",
                                   flight_id="flight-001", item_id="food-pack", quantity=60, priority=5)
        freeze_and_decide(service, clock)
        allocation = service.pending_load_confirmations("flight-001")["pending"][0]
        for _ in range(3):
            receipt = service.confirm_load(request_id="dd-confirm", actor_id="op-001",
                                           allocation_id=allocation["allocation_id"], expected_version=1)
        self.assertTrue(receipt.replayed)
        row = database.connection.execute(
            "SELECT status, version FROM log_allocations WHERE allocation_id=?",
            (allocation["allocation_id"],)).fetchone()
        self.assertEqual("loaded", row["status"])
        self.assertEqual(2, row["version"])
        report = service.conservation_report("flight-001")
        food_hold = next(h for h in report["holds"] if h["code"] == "HA")
        self.assertAlmostEqual(120.0, food_hold["allocated_weight_kg"])


if __name__ == "__main__":
    unittest.main()
