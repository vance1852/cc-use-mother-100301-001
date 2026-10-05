import unittest

from polar_station_foundation.service import DomainService

from logistics_fixtures import MutableClock, new_world
from polar_station_logistics.api import route


class LogisticsApiTest(unittest.TestCase):
    """物流决策 HTTP 路由层。"""

    def setUp(self):
        self.service, self.foundation, self.clock, self.database = new_world()

    def tearDown(self):
        self.database.close()

    def _post(self, path, body, actor="op-001"):
        return route(self.service, self.foundation, "POST", path, body, {"X-Actor-Id": actor})

    def _get(self, path, actor="admin-001"):
        return route(self.service, self.foundation, "GET", path, None, {"X-Actor-Id": actor})

    def test_foundation_routes_still_work(self):
        status, payload = self._get("/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_logistics_route_returns_404(self):
        status, payload = self._get("/logistics/nope")
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_actor_is_rejected(self):
        status, payload = route(self.service, self.foundation, "POST", "/logistics/items",
                                {"request_id": "api-x", "item_id": "thing", "site_id": "site-001",
                                 "category": "food", "name": "物资", "hazard_level": "general",
                                 "unit_weight_kg": 1.0, "unit_volume_m3": 0.1}, {})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_item_creation_and_replay_status_codes(self):
        body = {"request_id": "api-item", "item_id": "med-kit", "site_id": "site-001",
                "category": "medical", "name": "医疗包", "hazard_level": "general",
                "unit_weight_kg": 4.0, "unit_volume_m3": 0.05}
        status, payload = self._post("/logistics/items", body)
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = self._post("/logistics/items", body)
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_validation_error_maps_to_400(self):
        status, payload = self._post("/logistics/items",
                                     {"request_id": "api-bad", "item_id": "bad item!",
                                      "site_id": "site-001", "category": "food", "name": "坏",
                                      "hazard_level": "general", "unit_weight_kg": 1.0,
                                      "unit_volume_m3": 0.1})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_full_flow_over_http(self):
        self._post("/logistics/declarations",
                   {"request_id": "api-d1", "declaration_id": "D-API", "flight_id": "flight-001",
                    "item_id": "food-pack", "quantity": 60, "priority": 5})
        self.clock.advance(seconds=17 * 3600)
        status, _ = self._post("/logistics/flights/flight-001/freeze",
                               {"request_id": "api-freeze"}, actor="admin-001")
        self.assertEqual(201, status)
        status, _ = self._post("/logistics/flights/flight-001/decide",
                               {"request_id": "api-decide"}, actor="admin-001")
        self.assertEqual(201, status)
        status, payload = self._get("/logistics/flights/flight-001/conservation")
        self.assertEqual(200, status)
        self.assertTrue(payload["all_ok"])
        status, payload = self._get("/logistics/declarations/D-API/explain")
        self.assertEqual(200, status)
        self.assertEqual("approved", payload["decisions"][0]["status"])
        status, payload = self._get("/logistics/flights/flight-001/pending-loads")
        self.assertEqual(200, status)
        self.assertEqual(1, payload["pending_count"])
        allocation = payload["pending"][0]
        status, _ = self._post(f"/logistics/allocations/{allocation['allocation_id']}/confirm-load",
                               {"request_id": "api-confirm", "expected_version": allocation["version"]})
        self.assertEqual(201, status)
        status, _ = self._post("/logistics/flights/flight-001/seal",
                               {"request_id": "api-seal"}, actor="admin-001")
        self.assertEqual(201, status)
        status, payload = self._get("/logistics/flights/flight-001")
        self.assertEqual("closed", payload["flight"]["status"])

    def test_emergency_flow_over_http(self):
        self._post("/logistics/declarations",
                   {"request_id": "api-e0", "declaration_id": "D-APIE", "flight_id": "flight-001",
                    "item_id": "food-pack", "quantity": 60, "priority": 5})
        self.clock.advance(seconds=17 * 3600)
        self._post("/logistics/flights/flight-001/freeze", {"request_id": "api-fr"}, actor="admin-001")
        self._post("/logistics/flights/flight-001/decide", {"request_id": "api-de"}, actor="admin-001")
        status, _ = self._post("/logistics/emergency-requests",
                               {"request_id": "api-em", "emergency_id": "EM-API",
                                "flight_id": "flight-001", "item_id": "o2-bottle", "quantity": 2,
                                "justification": "氧气异常", "approval_window_seconds": 600},
                               actor="rev-1")
        self.assertEqual(201, status)
        self._post("/logistics/emergency-requests/EM-API/approvals",
                   {"request_id": "api-em-a1"}, actor="admin-001")
        status, payload = self._get("/logistics/emergency-requests/EM-API")
        self.assertEqual("pending", payload["emergency"]["status"])
        self._post("/logistics/emergency-requests/EM-API/approvals",
                   {"request_id": "api-em-a2"})
        status, payload = self._get("/logistics/emergency-requests/EM-API")
        self.assertEqual("approved", payload["emergency"]["status"])
        self.assertEqual(1, len(payload["allocations"]))


if __name__ == "__main__":
    unittest.main()
