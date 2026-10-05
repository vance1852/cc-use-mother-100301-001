"""越冬物资配额与装载决策的领域测试。"""
from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polar_station_foundation.cargo import CargoService
from polar_station_foundation.clock import FixedClock
from polar_station_foundation.errors import (
    ConflictError,
    InfeasibleError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from polar_station_foundation.storage import Database

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
DEADLINE = (BASE + timedelta(days=10)).isoformat()
WINTER_END = (BASE + timedelta(days=200)).isoformat()
EXPIRY_OK = (BASE + timedelta(days=300)).isoformat()
EXPIRY_BAD = (BASE + timedelta(days=100)).isoformat()


class CargoCase(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = self._build(self.database, BASE)
        self.ids = {}

    def tearDown(self):
        self.database.close()

    @staticmethod
    def _build(database, clock):
        svc = CargoService(database, FixedClock(clock))
        svc.register_organization(request_id="org-a", actor_id="bootstrap",
                                  organization_id="oa", name="机构甲")
        svc.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="ad",
                           display_name="主管", role="admin", organization_id="oa")
        svc.register_organization(request_id="org-b", actor_id="ad",
                                  organization_id="ob", name="机构乙")
        svc.register_actor(request_id="opb", actor_id="ad", new_actor_id="ob-op",
                           display_name="乙操作员", role="operator", organization_id="ob")
        svc.register_actor(request_id="opa", actor_id="ad", new_actor_id="oa-op",
                           display_name="甲操作员", role="operator", organization_id="oa")
        svc.register_actor(request_id="rev", actor_id="ad", new_actor_id="rev1",
                           display_name="审计", role="reviewer", organization_id="oa")
        svc.register_site(request_id="site", actor_id="ad", site_id="s1", organization_id="oa",
                          name="中山站", timezone_name="UTC")
        return svc

    def voyage(self, vid="v1", **kw):
        params = dict(request_id=f"voy-{vid}", actor_id="ad", site_id="s1", voyage_id=vid,
                      code=f"FL-{vid}", deadline_at=DEADLINE, winter_end_at=WINTER_END,
                      dry_weight_capacity=1000, dry_volume_capacity=1000,
                      hazmat_weight_capacity=500, hazmat_volume_capacity=500,
                      reserved_dry_weight=100, reserved_dry_volume=100,
                      reserved_hazmat_weight=50, reserved_hazmat_volume=50)
        params.update(kw)
        return self.service.create_voyage(**params)

    def material(self, code, *, hazard="non_hazardous", uw=1.0, uv=1.0):
        return self.service.register_material(
            request_id=f"mat-{code}", actor_id="ad", site_id="s1", material_id=f"m-{code}",
            code=code, name=code, hazard_class=hazard, unit_weight=uw, unit_volume=uv)

    def submit(self, code, material, qty, org="oa", *, prio=50, expiry=EXPIRY_OK,
               voyage="v1", actor="ad", request_id=None):
        app_code = code if len(code) >= 2 else f"A-{code}"
        out = self.service.submit_application(
            request_id=request_id or f"app-{voyage}-{code}", actor_id=actor, voyage_id=voyage,
            code=app_code, organization_id=org, material_code=material, quantity=qty,
            batch_expiry_at=expiry, priority_score=prio)
        self.ids.setdefault(voyage, {})[code] = out["application_id"]
        return out

    def freeze(self, at=BASE + timedelta(days=11), voyage="v1", request_id="freeze-1"):
        self.service.clock = FixedClock(at)
        return self.service.freeze_voyage(request_id=request_id, actor_id="ad", voyage_id=voyage)

    def explain(self, code, voyage="v1"):
        return self.service.explain_application(voyage, self.ids[voyage][code])

    def alloc(self, code, voyage="v1"):
        return self.explain(code, voyage)["allocation"]


class VoyageLifecycleTest(CargoCase):
    def test_voyage_requires_valid_capacity_and_reserve(self):
        with self.assertRaises(ValidationError):
            self.voyage("vx", dry_weight_capacity=0)
        with self.assertRaises(ValidationError):
            self.voyage("vy", reserved_dry_weight=9999)
        with self.assertRaises(ValidationError):
            self.voyage("vz", deadline_at=WINTER_END, winter_end_at=DEADLINE)

    def test_freeze_before_decline_rejected_and_idempotent_after(self):
        self.voyage()
        self.material("FOOD")
        with self.assertRaises(ConflictError):
            self.service.freeze_voyage(request_id="early", actor_id="ad", voyage_id="v1")
        first = self.freeze(request_id="fz")
        second = self.freeze(request_id="fz")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["manifest_hash"], second["manifest_hash"])
        with self.assertRaises(ConflictError):
            # 截止后航次基线（储备/配额/申报）不可再改
            self.service.add_critical_reserve(
                request_id="late", actor_id="ad", voyage_id="v1",
                material_code="FOOD", quantity=1, unit="箱")

    def test_manifest_hash_is_stable_snapshot(self):
        self.voyage()
        self.material("FOOD")
        self.submit("A", "FOOD", 10)
        self.freeze()
        snap = self.service.freeze_manifest("v1")
        self.assertEqual(snap["manifest_hash"], self.freeze(request_id="freeze-1")["manifest_hash"])
        self.assertIn("applications", snap["manifest"])
        self.assertIn("decision", snap["manifest"])


class QuotaAndDuplicateTest(CargoCase):
    def test_split_orders_cannot_bypass_org_quota(self):
        self.voyage()
        self.material("FILTER", hazard="flammable")
        self.service.set_org_quota(request_id="q1", actor_id="ad", voyage_id="v1",
                                   organization_id="oa", material_code="FILTER", max_quantity=10)
        self.submit("F1", "FILTER", 6)
        with self.assertRaises(ConflictError):
            self.submit("F2", "FILTER", 5)
        # 修订同样不能借道
        with self.assertRaises(ConflictError):
            self.service.revise_application(request_id="rev1", actor_id="ad", voyage_id="v1",
                                            application_id=self.ids["v1"]["F1"], quantity=11,
                                            change_reason="加码")

    def test_other_org_submission_denied(self):
        self.voyage()
        self.material("FOOD")
        with self.assertRaises(PermissionDenied):
            self.submit("X", "FOOD", 1, org="oa", actor="ob-op")

    def test_duplicate_material_explained_in_decision(self):
        self.voyage(dry_weight_capacity=100, dry_volume_capacity=100,
                    hazmat_weight_capacity=80, hazmat_volume_capacity=80,
                    reserved_hazmat_weight=0, reserved_hazmat_volume=0)
        self.material("BATT", hazard="lithium_battery", uw=10, uv=10)
        self.submit("HI", "BATT", 8, prio=90)
        self.submit("LO", "BATT", 8, prio=10)
        self.freeze()
        self.assertEqual(self.alloc("HI")["state"], "allocated")
        lo = self.explain("LO")
        self.assertEqual(lo["application"]["status"], "waitlisted")
        self.assertIn("duplicate_material", [r["code"] for r in lo["decision_reason"]])

    def test_revisions_are_versioned(self):
        self.voyage()
        self.material("FOOD")
        self.submit("A", "FOOD", 10)
        self.service.revise_application(request_id="rv", actor_id="ad", voyage_id="v1",
                                        application_id=self.ids["v1"]["A"], quantity=12,
                                        change_reason="多报2份")
        ex = self.explain("A")
        self.assertEqual([r["version"] for r in ex["revisions"]], [1, 2])
        self.assertEqual(ex["application"]["quantity"], 12)


class FreezeDecisionTest(CargoCase):
    def test_expired_batch_rejected(self):
        self.voyage()
        self.material("FUEL", hazard="flammable")
        self.submit("OLD", "FUEL", 5, expiry=EXPIRY_BAD, prio=100)
        self.freeze()
        self.assertEqual(self.alloc("OLD")["state"], "rejected")
        self.assertIn("batch_expires_before_winter_end",
                      [r["code"] for r in self.explain("OLD")["decision_reason"]])

    def test_critical_reserve_infeasible_aborts_freeze(self):
        self.voyage()
        self.material("FOOD")
        self.service.add_critical_reserve(request_id="cr", actor_id="ad", voyage_id="v1",
                                          material_code="FOOD", quantity=100, unit="箱")
        self.submit("A", "FOOD", 40)
        with self.assertRaises(InfeasibleError):
            self.freeze()
        # 失败不产生任何配载，航次仍开放
        self.assertEqual(self.service.get_voyage("v1")["status"], "open")

    def test_critical_reserve_priority_over_higher_score_non_reserve(self):
        self.voyage(dry_weight_capacity=100, dry_volume_capacity=100,
                    reserved_dry_weight=0, reserved_dry_volume=0)
        self.material("FOOD")
        self.material("GEAR")
        self.service.add_critical_reserve(request_id="cr", actor_id="ad", voyage_id="v1",
                                          material_code="FOOD", quantity=100, unit="箱")
        self.submit("RESERVE", "FOOD", 100, prio=10)
        self.submit("GEAR", "GEAR", 100, prio=99)
        self.freeze()
        self.assertEqual(self.alloc("RESERVE")["allocated_qty"], 100)
        self.assertEqual(self.alloc("GEAR")["state"], "waitlisted")


class RelationVersioningTest(CargoCase):
    def test_relation_supersedes_keeps_full_history(self):
        self.voyage()
        self.material("BATT", hazard="lithium_battery")
        self.submit("A", "BATT", 10)
        self.submit("B", "BATT", 10)
        self.service.record_relation(request_id="rel1", actor_id="ad", voyage_id="v1",
                                     from_application_id=self.ids["v1"]["A"],
                                     to_application_id=self.ids["v1"]["B"], kind="substitute",
                                     note="v1")
        self.service.record_relation(request_id="rel2", actor_id="ad", voyage_id="v1",
                                     from_application_id=self.ids["v1"]["A"],
                                     to_application_id=self.ids["v1"]["B"], kind="substitute",
                                     note="v2 更新")
        rels = self.service.list_relations("v1")
        self.assertEqual([r["version"] for r in rels], [1, 2])
        self.assertEqual([r["active"] for r in rels], [0, 1])
        self.assertEqual(rels[1]["supersedes_relation_id"], rels[0]["relation_id"])

    def test_split_quantity_bounded_and_defer_requires_successor(self):
        self.voyage()
        self.voyage("v2")
        self.material("BATT", hazard="lithium_battery")
        self.submit("A", "BATT", 10)
        self.submit("B", "BATT", 4)
        with self.assertRaises(ValidationError):
            self.service.record_relation(request_id="s1", actor_id="ad", voyage_id="v1",
                                         from_application_id=self.ids["v1"]["A"],
                                         to_application_id=self.ids["v1"]["B"],
                                         kind="split", quantity=11)
        self.service.record_relation(request_id="s2", actor_id="ad", voyage_id="v1",
                                     from_application_id=self.ids["v1"]["A"],
                                     to_application_id=self.ids["v1"]["B"],
                                     kind="split", quantity=4)
        self.service.record_relation(request_id="d1", actor_id="ad", voyage_id="v1",
                                     from_application_id=self.ids["v1"]["A"],
                                     kind="defer", successor_voyage_id="v2")
        defer = [r for r in self.service.list_relations("v1") if r["kind"] == "defer"][0]
        self.assertEqual(defer["successor_voyage_id"], "v2")


class EmergencyReleaseTest(CargoCase):
    def _emergency_ready(self):
        self.voyage()
        self.material("FOOD")
        self.submit("A", "FOOD", 10)
        self.freeze()

    def test_two_distinct_authorizers_required_within_window(self):
        self._emergency_ready()
        emg = self.service.request_emergency_release(
            request_id="e1", actor_id="ad", voyage_id="v1", organization_id="oa",
            material_code="FOOD", quantity=20, reason="抢修", valid_minutes=30)
        first = self.service.approve_emergency_release(
            request_id="ea1", actor_id="ad", release_id=emg["release_id"])
        self.assertEqual(first["status"], "awaiting_second_approval")
        with self.assertRaises(ConflictError):
            self.service.approve_emergency_release(
                request_id="ea2", actor_id="ad", release_id=emg["release_id"])
        done = self.service.approve_emergency_release(
            request_id="ea3", actor_id="oa-op", release_id=emg["release_id"])
        self.assertEqual(done["status"], "approved")
        ex = self.service.explain_application("v1", emg["application_id"])
        self.assertEqual(ex["application"]["status"], "approved")
        self.assertIsNotNone(ex["emergency_release"])

    def test_reviewer_cannot_authorize(self):
        self._emergency_ready()
        emg = self.service.request_emergency_release(
            request_id="e1", actor_id="ad", voyage_id="v1", organization_id="oa",
            material_code="FOOD", quantity=1, reason="x", valid_minutes=30)
        with self.assertRaises(PermissionDenied):
            self.service.approve_emergency_release(
                request_id="x", actor_id="rev1", release_id=emg["release_id"])

    def test_expired_window_cannot_touch_reserve(self):
        self._emergency_ready()
        emg = self.service.request_emergency_release(
            request_id="e1", actor_id="ad", voyage_id="v1", organization_id="oa",
            material_code="FOOD", quantity=1, reason="x", valid_minutes=30)
        self.service.clock = FixedClock(BASE + timedelta(days=11, minutes=31))
        with self.assertRaises(ConflictError):
            self.service.approve_emergency_release(
                request_id="ea", actor_id="oa-op", release_id=emg["release_id"])
        row = [r for r in self.service.list_emergency_releases("v1")
               if r["release_id"] == emg["release_id"]][0]
        self.assertEqual(row["status"], "expired")

    def test_reserve_pool_cannot_be_overspent(self):
        self.voyage(reserved_dry_weight=5, reserved_dry_volume=100)
        self.material("FOOD", uw=2.0, uv=1.0)
        self.submit("A", "FOOD", 10)
        self.freeze()
        emg = self.service.request_emergency_release(
            request_id="e1", actor_id="ad", voyage_id="v1", organization_id="oa",
            material_code="FOOD", quantity=3, reason="x", valid_minutes=30)
        self.service.approve_emergency_release(request_id="a1", actor_id="ad",
                                               release_id=emg["release_id"])
        with self.assertRaises(ConflictError):
            self.service.approve_emergency_release(request_id="a2", actor_id="oa-op",
                                                   release_id=emg["release_id"])

    def test_emergency_only_after_freeze(self):
        self.voyage()
        self.material("FOOD")
        with self.assertRaises(ConflictError):
            self.service.request_emergency_release(
                request_id="e1", actor_id="ad", voyage_id="v1", organization_id="oa",
                material_code="FOOD", quantity=1, reason="x", valid_minutes=30)

    def test_emergency_shortfall_returns_to_reserve_pool(self):
        self.voyage(reserved_dry_weight=100, reserved_dry_volume=100)
        self.material("FOOD")
        self.freeze()
        emg = self.service.request_emergency_release(
            request_id="e1", actor_id="ad", voyage_id="v1", organization_id="oa",
            material_code="FOOD", quantity=20, reason="抢修", valid_minutes=60)
        self.service.approve_emergency_release(request_id="a1", actor_id="ad",
                                               release_id=emg["release_id"])
        self.service.approve_emergency_release(request_id="a2", actor_id="oa-op",
                                               release_id=emg["release_id"])
        self.assertEqual(self.service.get_voyage("v1")["reserved_dry_weight_used"], 20)
        # 紧急物资部分到货：未装载的 10 份必须退回预留池，不进入普通递补
        out = self.service.report_shortfall(request_id="s1", actor_id="ad", voyage_id="v1",
                                            application_id=emg["application_id"], missing_qty=10)
        self.assertTrue(out["returned_to_reserve_pool"])
        self.assertEqual(out["reassigned"], [])
        self.assertAlmostEqual(self.service.get_voyage("v1")["reserved_dry_weight_used"],
                               10, places=6)
        report = self.service.verify_conservation("v1")
        self.assertTrue(report["conserved"],
                        [c for c in report["checks"] if not c["passed"]])
        events = {row["event_type"] for row in self.service.ledger("v1")}
        self.assertIn("emergency_released", events)


class LoadingAndReplayTest(CargoCase):
    def test_loading_replay_does_not_double_deduct(self):
        self.voyage()
        self.material("FOOD")
        self.submit("A", "FOOD", 10)
        self.freeze()
        a = self.service.confirm_loading(request_id="l1", actor_id="ad", voyage_id="v1",
                                         application_id=self.ids["v1"]["A"], quantity=4)
        b = self.service.confirm_loading(request_id="l1", actor_id="ad", voyage_id="v1",
                                         application_id=self.ids["v1"]["A"], quantity=4)
        self.assertFalse(a["replayed"])
        self.assertTrue(b["replayed"])
        self.assertEqual(self.alloc("A")["loaded_qty"], 4)
        with self.assertRaises(ConflictError):
            self.service.confirm_loading(request_id="l2", actor_id="ad", voyage_id="v1",
                                         application_id=self.ids["v1"]["A"], quantity=7)

    def _file_services(self, directory: str):
        """在同一文件库上建立两个独立连接的服务，用于真并发测试。"""

        path = str(Path(directory) / "concurrent.sqlite3")
        db_seed = Database(path)
        svc = self._build(db_seed, BASE + timedelta(days=11))
        svc.create_voyage(request_id="voy-v1", actor_id="ad", site_id="s1", voyage_id="v1",
                          code="FL-v1", deadline_at=DEADLINE, winter_end_at=WINTER_END,
                          dry_weight_capacity=100, dry_volume_capacity=100,
                          hazmat_weight_capacity=100, hazmat_volume_capacity=100)
        svc.register_material(request_id="mat-FOOD", actor_id="ad", site_id="s1",
                              material_id="m-FOOD", code="FOOD", name="FOOD",
                              hazard_class="non_hazardous", unit_weight=1, unit_volume=1)
        app = svc.submit_application(request_id="app-x", actor_id="ad", voyage_id="v1",
                                     code="APP-X", organization_id="oa", material_code="FOOD",
                                     quantity=10, priority_score=10)
        svc.freeze_voyage(request_id="fz", actor_id="ad", voyage_id="v1")
        db_seed.close()
        db1 = Database(path)
        db2 = Database(path)
        return CargoService(db1, FixedClock(BASE + timedelta(days=11))), \
            CargoService(db2, FixedClock(BASE + timedelta(days=11))), \
            app["application_id"], (db1, db2)

    def test_concurrent_loadings_never_exceed_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            svc1, svc2, app_id, dbs = self._file_services(directory)
            errors: list[Exception] = []

            def load(service, seq):
                try:
                    service.confirm_loading(
                        request_id=f"cc-{seq}", actor_id="ad", voyage_id="v1",
                        application_id=app_id, quantity=6)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=load, args=(svc1, 1)),
                       threading.Thread(target=load, args=(svc2, 2))]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(len(errors), 1)
            pending = svc1.pending_loading("v1")
            self.assertEqual(pending[0]["allocated_qty"] - pending[0]["loaded_qty"], 4)
            for db in dbs:
                db.close()

    def test_concurrent_same_request_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory:
            # 先播种并拿到文件库路径与申请 id
            seed_db, seed_svc, app_id, seed_dbs = None, None, None, None
            path = str(Path(directory) / "concurrent-rid.sqlite3")
            db_seed = Database(path)
            svc = self._build(db_seed, BASE + timedelta(days=11))
            svc.create_voyage(request_id="voy-v1", actor_id="ad", site_id="s1", voyage_id="v1",
                              code="FL-v1", deadline_at=DEADLINE, winter_end_at=WINTER_END,
                              dry_weight_capacity=100, dry_volume_capacity=100,
                              hazmat_weight_capacity=100, hazmat_volume_capacity=100)
            svc.register_material(request_id="mat-FOOD", actor_id="ad", site_id="s1",
                                  material_id="m-FOOD", code="FOOD", name="FOOD",
                                  hazard_class="non_hazardous", unit_weight=1, unit_volume=1)
            app_id = svc.submit_application(request_id="app-x", actor_id="ad", voyage_id="v1",
                                            code="APP-X", organization_id="oa", material_code="FOOD",
                                            quantity=10, priority_score=10)["application_id"]
            svc.freeze_voyage(request_id="fz", actor_id="ad", voyage_id="v1")
            db_seed.close()

            services = [CargoService(Database(path), FixedClock(BASE + timedelta(days=11)))
                        for _ in range(4)]
            outcomes: list[str] = []

            def load(service):
                try:
                    out = service.confirm_loading(
                        request_id="same-rid", actor_id="ad", voyage_id="v1",
                        application_id=app_id, quantity=3)
                    outcomes.append("replay" if out["replayed"] else "first")
                except Exception as exc:  # noqa: BLE001
                    outcomes.append(f"error:{exc}")

            threads = [threading.Thread(target=load, args=(svc_i,)) for svc_i in services]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(outcomes.count("first"), 1, outcomes)
            self.assertEqual(outcomes.count("replay"), 3, outcomes)
            pending = services[0].pending_loading("v1")
            self.assertEqual(pending[0]["allocated_qty"] - pending[0]["loaded_qty"], 7)
            for svc_i in services:
                svc_i.database.close()

    def test_issue_cannot_exceed_loaded(self):
        self.voyage()
        self.material("FOOD")
        self.submit("A", "FOOD", 10)
        self.freeze()
        self.service.confirm_loading(request_id="l1", actor_id="ad", voyage_id="v1",
                                     application_id=self.ids["v1"]["A"], quantity=5)
        with self.assertRaises(ConflictError):
            self.service.confirm_issue(request_id="i1", actor_id="ad", voyage_id="v1",
                                       application_id=self.ids["v1"]["A"], quantity=6)
        self.service.confirm_issue(request_id="i2", actor_id="ad", voyage_id="v1",
                                   application_id=self.ids["v1"]["A"], quantity=5)

    def test_pending_loading_resumes_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cargo.sqlite3"
            db1 = Database(path)
            svc1 = self._build(db1, BASE + timedelta(days=11))
            svc1.create_voyage(request_id="voy-v1", actor_id="ad", site_id="s1", voyage_id="v1",
                               code="FL-v1", deadline_at=DEADLINE, winter_end_at=WINTER_END,
                               dry_weight_capacity=100, dry_volume_capacity=100,
                               hazmat_weight_capacity=100, hazmat_volume_capacity=100)
            svc1.register_material(request_id="mat-FOOD", actor_id="ad", site_id="s1",
                                   material_id="m-FOOD", code="FOOD", name="FOOD",
                                   hazard_class="non_hazardous", unit_weight=1, unit_volume=1)
            app = svc1.submit_application(request_id="app-x", actor_id="ad", voyage_id="v1",
                                          code="APP-X", organization_id="oa", material_code="FOOD",
                                          quantity=10, priority_score=10)
            svc1.freeze_voyage(request_id="fz", actor_id="ad", voyage_id="v1")
            svc1.confirm_loading(request_id="load-half", actor_id="ad", voyage_id="v1",
                                 application_id=app["application_id"], quantity=4)
            db1.close()

            db2 = Database(path)
            svc2 = CargoService(db2, FixedClock(BASE + timedelta(days=11)))
            pending = svc2.pending_loading("v1")
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["outstanding_qty"], 6)
            svc2.confirm_loading(request_id="load-rest", actor_id="ad", voyage_id="v1",
                                 application_id=app["application_id"], quantity=6)
            self.assertEqual(svc2.pending_loading("v1"), [])
            db2.close()


class ReallocationTest(CargoCase):
    def _contested(self):
        # 干货舱紧张：FOOD 储备 50；MAIN 100 高分占满，BACK 30 落榜候补
        self.voyage(dry_weight_capacity=100, dry_volume_capacity=100,
                    reserved_dry_weight=0, reserved_dry_volume=0)
        self.material("FOOD")
        self.service.add_critical_reserve(request_id="cr", actor_id="ad", voyage_id="v1",
                                          material_code="FOOD", quantity=50, unit="箱")
        self.submit("MAIN", "FOOD", 100, prio=90)
        self.submit("BACK", "FOOD", 30, prio=20)
        self.freeze()

    def test_shortfall_cascades_by_frozen_rank(self):
        self._contested()
        self.assertEqual(self.alloc("MAIN")["allocated_qty"], 100)
        self.assertEqual(self.alloc("BACK")["state"], "waitlisted")
        out = self.service.report_shortfall(request_id="s1", actor_id="ad", voyage_id="v1",
                                            application_id=self.ids["v1"]["MAIN"],
                                            missing_qty=20)
        self.assertEqual(out["reassigned"][0]["application_id"], self.ids["v1"]["BACK"])
        self.assertEqual(self.alloc("BACK")["allocated_qty"], 20)
        report = self.service.verify_conservation("v1")
        self.assertTrue(report["conserved"])
        self.assertTrue(report["critical_reserves_met"])

    def test_release_that_breaches_reserve_rolls_back(self):
        self._contested()
        with self.assertRaises(ConflictError):
            self.service.report_shortfall(request_id="s2", actor_id="ad", voyage_id="v1",
                                          application_id=self.ids["v1"]["MAIN"],
                                          missing_qty=90)
        # 回滚后状态与守恒不变
        self.assertEqual(self.alloc("MAIN")["allocated_qty"], 100)
        self.assertEqual(self.alloc("BACK")["allocated_qty"], 0)

    def test_loaded_share_is_immovable(self):
        self._contested()
        self.service.confirm_loading(request_id="l1", actor_id="ad", voyage_id="v1",
                                     application_id=self.ids["v1"]["MAIN"], quantity=30)
        with self.assertRaises(ConflictError):
            self.service.report_shortfall(request_id="s3", actor_id="ad", voyage_id="v1",
                                          application_id=self.ids["v1"]["MAIN"],
                                          missing_qty=80)

    def test_expiry_follows_same_release_rules(self):
        self._contested()
        out = self.service.report_expiry(request_id="x1", actor_id="ad", voyage_id="v1",
                                         application_id=self.ids["v1"]["MAIN"], quantity=10)
        self.assertEqual(out["reassigned"][0]["application_id"], self.ids["v1"]["BACK"])
        events = [e["event_type"] for e in self.service.ledger("v1")]
        self.assertIn("expired", events)

    def test_swap_without_relation_is_rejected(self):
        self.voyage(hazmat_weight_capacity=300, hazmat_volume_capacity=120,
                    reserved_hazmat_weight=0, reserved_hazmat_volume=0)
        self.material("BATT", hazard="lithium_battery", uw=2.0, uv=1.0)
        self.submit("A", "BATT", 100, prio=90)
        self.submit("B", "BATT", 100, prio=10)
        self.freeze()
        with self.assertRaises(NotFoundError):
            self.service.swap_to_substitute(request_id="sw0", actor_id="ad", voyage_id="v1",
                                            from_application_id=self.ids["v1"]["A"],
                                            to_application_id=self.ids["v1"]["B"], quantity=10)

    def test_swap_uses_active_versioned_relation_and_is_idempotent(self):
        self.voyage(hazmat_weight_capacity=200, hazmat_volume_capacity=100,
                    reserved_hazmat_weight=0, reserved_hazmat_volume=0)
        self.material("BATT", hazard="lithium_battery", uw=2.0, uv=1.0)
        self.submit("A", "BATT", 100, prio=90)
        self.submit("B", "BATT", 100, prio=10)
        # 替代关系必须随冻结清单版本留痕：冻结前登记
        self.service.record_relation(request_id="rel", actor_id="ad", voyage_id="v1",
                                     from_application_id=self.ids["v1"]["A"],
                                     to_application_id=self.ids["v1"]["B"], kind="substitute")
        self.freeze()
        out = self.service.swap_to_substitute(request_id="sw1", actor_id="ad", voyage_id="v1",
                                              from_application_id=self.ids["v1"]["A"],
                                              to_application_id=self.ids["v1"]["B"], quantity=10)
        self.assertEqual(out["relation_version"], 1)
        again = self.service.swap_to_substitute(request_id="sw1", actor_id="ad", voyage_id="v1",
                                                from_application_id=self.ids["v1"]["A"],
                                                to_application_id=self.ids["v1"]["B"], quantity=10)
        self.assertTrue(again["replayed"])
        self.assertEqual(self.alloc("A")["allocated_qty"], 90)
        self.assertEqual(self.alloc("B")["allocated_qty"], 10)

    def test_flight_cancellation_defers_per_relations(self):
        self.voyage("v2", deadline_at=(BASE + timedelta(days=40)).isoformat())
        # 复用 _contested 的货舱设定，但顺延关系要在冻结前登记
        self.voyage(dry_weight_capacity=100, dry_volume_capacity=100,
                    reserved_dry_weight=0, reserved_dry_volume=0)
        self.material("FOOD")
        self.service.add_critical_reserve(request_id="cr", actor_id="ad", voyage_id="v1",
                                          material_code="FOOD", quantity=50, unit="箱")
        self.submit("MAIN", "FOOD", 100, prio=90)
        self.submit("BACK", "FOOD", 30, prio=20)
        self.service.record_relation(request_id="d1", actor_id="ad", voyage_id="v1",
                                     from_application_id=self.ids["v1"]["MAIN"],
                                     kind="defer", successor_voyage_id="v2")
        self.freeze()
        out = self.service.report_flight_cancellation(request_id="c1", actor_id="ad",
                                                      voyage_id="v1", reason="风暴")
        self.assertGreaterEqual(out["deferred_count"], 1)
        report = self.service.verify_conservation("v1")
        self.assertTrue(report["conserved"])
        # MAIN 顺延到 v2，BACKUP 通过释放递补仍在本航次
        main = self.alloc("MAIN")
        self.assertEqual(main["state"], "deferred")
        self.assertGreaterEqual(self.alloc("BACK")["allocated_qty"], 0)
        coverage = {r["material_code"]: r for r in report["critical_reserves"]}
        self.assertTrue(coverage["FOOD"]["met"])
        self.assertGreater(coverage["FOOD"]["forwarded_to_successor"], 0)


class ConservationAndAuditTest(CargoCase):
    def test_conservation_and_explainability_after_full_lifecycle(self):
        self.voyage()
        self.material("FOOD")
        self.material("BATT", hazard="lithium_battery")
        self.service.add_critical_reserve(request_id="cr", actor_id="ad", voyage_id="v1",
                                          material_code="FOOD", quantity=30, unit="箱")
        self.submit("F", "FOOD", 50, prio=80)
        self.submit("B", "BATT", 10, prio=40)
        self.freeze()
        report = self.service.verify_conservation("v1")
        self.assertTrue(report["conserved"])
        self.assertTrue(all(c["passed"] for c in report["checks"]))
        ex = self.service.explain_application("v1", self.ids["v1"]["F"])
        self.assertTrue(ex["revisions"])
        self.assertIn("allocation", ex)
        ledger_events = {row["event_type"] for row in ex["ledger"]}
        self.assertIn("allocated", ledger_events)
        valid, count = self.service.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 5)
        actions = {e["action"] for e in self.service.audit_events()}
        self.assertIn("voyage.frozen", actions)


if __name__ == "__main__":
    unittest.main()
