"""越冬物资接口的路由层测试。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from polar_station_foundation.api import route
from polar_station_foundation.cargo import CargoService
from polar_station_foundation.clock import FixedClock
from polar_station_foundation.storage import Database

BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
DEADLINE = (BASE + timedelta(days=10)).isoformat()
WINTER_END = (BASE + timedelta(days=200)).isoformat()
EXPIRY_OK = (BASE + timedelta(days=300)).isoformat()

H = {"X-Actor-Id": "ad"}


class CargoApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = CargoService(self.database, FixedClock(BASE))
        route(self.service, "POST", "/organizations", {
            "request_id": "o1", "actor_id": "bootstrap", "organization_id": "oa", "name": "机构甲"})
        route(self.service, "POST", "/actors", {
            "request_id": "a1", "actor_id": "bootstrap", "new_actor_id": "ad",
            "display_name": "主管", "role": "admin", "organization_id": "oa"})
        route(self.service, "POST", "/actors", {
            "request_id": "a2", "actor_id": "ad", "new_actor_id": "op",
            "display_name": "操作员", "role": "operator", "organization_id": "oa"})
        route(self.service, "POST", "/sites", {
            "request_id": "s1", "actor_id": "ad", "site_id": "site1", "organization_id": "oa",
            "name": "中山站", "timezone_name": "UTC"})

    def tearDown(self):
        self.database.close()

    def _voyage(self):
        status, body = route(self.service, "POST", "/voyages", {
            "request_id": "v1", "actor_id": "ad", "site_id": "site1", "voyage_id": "voy1",
            "code": "FL-01", "deadline_at": DEADLINE, "winter_end_at": WINTER_END,
            "dry_weight_capacity": 200, "dry_volume_capacity": 200,
            "hazmat_weight_capacity": 200, "hazmat_volume_capacity": 200,
            "reserved_dry_weight": 100, "reserved_dry_volume": 100,
            "reserved_hazmat_weight": 50, "reserved_hazmat_volume": 50}, H)
        self.assertIn(status, (200, 201), body)

    def _material(self, code, hazard="non_hazardous"):
        status, body = route(self.service, "POST", "/materials", {
            "request_id": f"m-{code}", "actor_id": "ad", "site_id": "site1",
            "material_id": f"mat-{code}", "code": code, "name": code,
            "hazard_class": hazard, "unit_weight": 1.0, "unit_volume": 1.0}, H)
        self.assertIn(status, (200, 201), body)

    def test_full_lifecycle_over_http_routes(self):
        self._voyage()
        self._material("FOOD")
        self._material("BATT", "lithium_battery")

        status, _ = route(self.service, "POST", "/critical-reserves", {
            "request_id": "cr1", "actor_id": "ad", "voyage_id": "voy1",
            "material_code": "FOOD", "quantity": 50, "unit": "箱"}, H)
        self.assertEqual(200, status)

        status, body = route(self.service, "POST", "/applications", {
            "request_id": "app1", "actor_id": "ad", "voyage_id": "voy1", "code": "A-FOOD",
            "organization_id": "oa", "material_code": "FOOD", "quantity": 80,
            "batch_expiry_at": EXPIRY_OK, "priority_score": 80}, H)
        self.assertIn(status, (200, 201), body)
        food_id = body["application_id"]

        status, body = route(self.service, "POST", "/applications", {
            "request_id": "app2", "actor_id": "ad", "voyage_id": "voy1", "code": "B-BATT",
            "organization_id": "oa", "material_code": "BATT", "quantity": 50,
            "priority_score": 40}, H)
        self.assertIn(status, (200, 201), body)

        # 截止前冻结被拒
        status, body = route(self.service, "POST", "/voyages/freeze",
                             {"request_id": "fz-early", "actor_id": "ad", "voyage_id": "voy1"}, H)
        self.assertEqual(409, status)

        # 推进时钟到截止后
        self.service.clock = FixedClock(BASE + timedelta(days=11))
        status, body = route(self.service, "POST", "/voyages/freeze",
                             {"request_id": "fz1", "actor_id": "ad", "voyage_id": "voy1"}, H)
        self.assertEqual(200, status, body)
        manifest_hash = body["manifest_hash"]
        status, replay = route(self.service, "POST", "/voyages/freeze",
                               {"request_id": "fz1", "actor_id": "ad", "voyage_id": "voy1"}, H)
        self.assertTrue(replay["replayed"])
        self.assertEqual(manifest_hash, replay["manifest_hash"])

        # 解释
        status, ex = route(self.service, "GET",
                           f"/application-explanation?voyage_id=voy1&application_id={food_id}", None)
        self.assertEqual(200, status)
        self.assertTrue(ex["decision_reason"])

        # 装载确认 + 重放
        status, first = route(self.service, "POST", "/loading/confirm", {
            "request_id": "load1", "actor_id": "ad", "voyage_id": "voy1",
            "application_id": food_id, "quantity": 30}, H)
        self.assertEqual(200, status, first)
        status, again = route(self.service, "POST", "/loading/confirm", {
            "request_id": "load1", "actor_id": "ad", "voyage_id": "voy1",
            "application_id": food_id, "quantity": 30}, H)
        self.assertTrue(again["replayed"])
        self.assertEqual(30, again["loaded_qty"])

        # 待装接续
        status, pending = route(self.service, "GET", "/loading/pending?voyage_id=voy1", None)
        self.assertEqual(200, status)
        self.assertTrue(any(p["outstanding_qty"] > 0 for p in pending["items"]))

        # 守恒
        status, cons = route(self.service, "GET", "/conservation?voyage_id=voy1", None)
        self.assertEqual(200, status)
        self.assertTrue(cons["conserved"], cons)
        self.assertTrue(cons["critical_reserves_met"])

    def test_emergency_requires_two_approvals_over_http(self):
        self._voyage()
        self._material("FOOD")
        self.service.clock = FixedClock(BASE + timedelta(days=11))
        route(self.service, "POST", "/voyages/freeze",
              {"request_id": "fz1", "actor_id": "ad", "voyage_id": "voy1"}, H)

        status, emg = route(self.service, "POST", "/emergency-releases", {
            "request_id": "emg1", "actor_id": "ad", "voyage_id": "voy1",
            "organization_id": "oa", "material_code": "FOOD", "quantity": 10,
            "reason": "抢修", "valid_minutes": 30}, H)
        self.assertIn(status, (200, 201), emg)
        release_id = emg["release_id"]

        status, first = route(self.service, "POST", "/emergency-approvals",
                              {"request_id": "ap1", "actor_id": "ad", "release_id": release_id}, H)
        self.assertEqual(200, status)
        self.assertEqual("awaiting_second_approval", first["status"])
        # 同一人重复会签被拒
        status, dup = route(self.service, "POST", "/emergency-approvals",
                            {"request_id": "ap2", "actor_id": "ad", "release_id": release_id}, H)
        self.assertEqual(409, status, dup)
        # 第二个授权人（身份以 X-Actor-Id 请求头为准）
        status, second = route(self.service, "POST", "/emergency-approvals",
                               {"request_id": "ap3", "release_id": release_id},
                               {"X-Actor-Id": "op"})
        self.assertEqual(200, status)
        self.assertEqual("approved", second["status"])

        status, cons = route(self.service, "GET", "/conservation?voyage_id=voy1", None)
        self.assertTrue(cons["conserved"], cons)

    def test_validation_error_is_400(self):
        status, body = route(self.service, "POST", "/voyages", {
            "request_id": "bad", "actor_id": "ad", "site_id": "site1", "voyage_id": "voy-bad",
            "code": "FL", "deadline_at": DEADLINE, "winter_end_at": WINTER_END,
            "dry_weight_capacity": -1, "dry_volume_capacity": 1,
            "hazmat_weight_capacity": 1, "hazmat_volume_capacity": 1}, H)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", body["error"])

    def test_unknown_route_and_missing_actor(self):
        status, body = route(self.service, "GET", "/nope", None)
        self.assertEqual(404, status)
        self._voyage()
        status, body = route(self.service, "POST", "/materials", {
            "request_id": "x", "site_id": "site1", "material_id": "m1", "code": "FOOD",
            "name": "食品", "hazard_class": "non_hazardous",
            "unit_weight": 1, "unit_volume": 1}, {"X-Actor-Id": ""})
        self.assertEqual(404, status)  # 未知操作者


if __name__ == "__main__":
    unittest.main()
