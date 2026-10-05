import unittest

from polar_station_foundation.errors import ConflictError, PermissionDenied, ValidationError

from logistics_fixtures import decisions_by_declaration, freeze_and_decide, new_world


class DecisionRuleTest(unittest.TestCase):
    """装载决策引擎的冻结规则。"""

    def setUp(self):
        self.service, self.foundation, self.clock, self.database = new_world()

    def tearDown(self):
        self.database.close()

    def _submit(self, request_id, actor, declaration_id, item, quantity, priority, batch=None):
        return self.service.submit_declaration(
            request_id=request_id, actor_id=actor, declaration_id=declaration_id,
            flight_id="flight-001", item_id=item, quantity=quantity, priority=priority, batch_id=batch)

    def test_critical_reserve_guard_protects_food_and_oxygen(self):
        # 站方申报大量非关键物资，食品与应急氧气的保底空间必须被护栏留住
        self._submit("g1", "op-001", "D-G1", "fuel-filter", 1000, 3)
        self._submit("g2", "op-001", "D-G2", "fuel-filter", 100, 3)
        freeze_and_decide(self.service, self.clock)
        decisions = decisions_by_declaration(self.service)
        self.assertEqual("partially_approved", decisions["D-G1"]["status"])
        self.assertEqual(650, decisions["D-G1"]["approved_quantity"])
        self.assertEqual("partial_guard", decisions["D-G1"]["reason_code"])
        self.assertEqual("waitlisted", decisions["D-G2"]["status"])
        self.assertEqual("critical_reserve_guard", decisions["D-G2"]["reason_code"])
        report = self.service.conservation_report("flight-001")
        self.assertTrue(report["all_ok"])
        self.assertFalse(report["critical_minimums_met"])

    def test_institution_quota_cannot_be_bypassed_by_splitting(self):
        # 同一机构拆成四单申报，总额度仍然生效
        for index in range(4):
            self._submit(f"s{index}", "rev-2", f"D-S{index}", "fuel-filter", 50, 3, "FF-1")
        freeze_and_decide(self.service, self.clock)
        decisions = decisions_by_declaration(self.service)
        statuses = [decisions[f"D-S{index}"]["status"] for index in range(4)]
        self.assertEqual(["approved", "approved", "partially_approved", "rejected"], statuses)
        self.assertEqual(20, decisions["D-S2"]["approved_quantity"])
        self.assertEqual("institution_quota_exceeded", decisions["D-S3"]["reason_code"])
        quota = next(row for row in self.service.conservation_report("flight-001")["quotas"]
                     if row["organization_id"] == "org-lab-2")
        self.assertAlmostEqual(120.0, quota["used_weight_kg"])
        self.assertTrue(quota["ok"])

    def test_expired_batch_is_rejected(self):
        self._submit("b1", "rev-1", "D-B1", "cryo-battery", 10, 4, "CB-OLD")
        freeze_and_decide(self.service, self.clock)
        decisions = decisions_by_declaration(self.service)
        self.assertEqual("rejected", decisions["D-B1"]["status"])
        self.assertEqual("batch_expired", decisions["D-B1"]["reason_code"])

    def test_hazard_level_without_hold_is_rejected(self):
        self.service.register_supply_item(request_id="hz-item", actor_id="op-001", item_id="solvent",
                                          site_id="site-001", category="reagent", name="易燃溶剂",
                                          hazard_level="flammable", unit_weight_kg=1.0, unit_volume_m3=0.01)
        self._submit("h1", "rev-1", "D-H1", "solvent", 5, 3)
        freeze_and_decide(self.service, self.clock)
        decisions = decisions_by_declaration(self.service)
        self.assertEqual("rejected", decisions["D-H1"]["status"])
        self.assertEqual("hazard_not_allowed", decisions["D-H1"]["reason_code"])

    def test_substitute_replaces_failed_primary(self):
        self._submit("p1", "rev-1", "D-P1", "cryo-battery", 20, 4, "CB-OLD")
        self._submit("p2", "rev-1", "D-P2", "cryo-battery-v2", 20, 3)
        self.service.add_declaration_relation(request_id="rel-sub", actor_id="rev-1",
                                              from_declaration_id="D-P2", kind="substitute",
                                              to_declaration_id="D-P1", note="二型可替代一型")
        freeze_and_decide(self.service, self.clock)
        decisions = decisions_by_declaration(self.service)
        self.assertEqual("rejected", decisions["D-P1"]["status"])
        self.assertEqual("approved", decisions["D-P2"]["status"])

    def test_substitute_group_approves_only_one(self):
        self._submit("q1", "rev-1", "D-Q1", "cryo-battery", 100, 4, "CB-NEW")
        self._submit("q2", "rev-1", "D-Q2", "cryo-battery-v2", 100, 3)
        self.service.add_declaration_relation(request_id="rel-sub-2", actor_id="rev-1",
                                              from_declaration_id="D-Q2", kind="substitute",
                                              to_declaration_id="D-Q1")
        freeze_and_decide(self.service, self.clock)
        decisions = decisions_by_declaration(self.service)
        self.assertEqual("approved", decisions["D-Q1"]["status"])
        self.assertEqual("superseded", decisions["D-Q2"]["status"])
        self.assertEqual("replaced_by_substitute", decisions["D-Q2"]["reason_code"])

    def test_split_parent_superseded_and_children_capped(self):
        self._submit("t0", "rev-1", "D-T0", "fuel-filter", 100, 3, "FF-1")
        self._submit("t1", "rev-1", "D-T1", "fuel-filter", 60, 3, "FF-1")
        self._submit("t2", "rev-1", "D-T2", "fuel-filter", 60, 3, "FF-1")
        self.service.add_declaration_relation(request_id="rel-t1", actor_id="rev-1",
                                              from_declaration_id="D-T0", kind="split",
                                              to_declaration_id="D-T1")
        self.service.add_declaration_relation(request_id="rel-t2", actor_id="rev-1",
                                              from_declaration_id="D-T0", kind="split",
                                              to_declaration_id="D-T2")
        freeze_and_decide(self.service, self.clock)
        decisions = decisions_by_declaration(self.service)
        self.assertEqual("superseded", decisions["D-T0"]["status"])
        self.assertEqual("split_into_parts", decisions["D-T0"]["reason_code"])
        self.assertEqual("approved", decisions["D-T1"]["status"])
        self.assertEqual("rejected", decisions["D-T2"]["status"])
        self.assertEqual("split_exceeds_parent", decisions["D-T2"]["reason_code"])

    def test_defer_relation_carries_over_to_target_flight(self):
        self.service.register_flight(request_id="fx-flight-2", actor_id="op-001", flight_id="flight-002",
                                     site_id="site-001", code="WIN-02",
                                     cutoff_at="2026-11-02T00:00:00Z", arrival_at="2026-12-01T00:00:00Z")
        self._submit("u1", "rev-1", "D-U1", "fuel-filter", 10, 3, "FF-1")
        self.service.add_declaration_relation(request_id="rel-defer", actor_id="rev-1",
                                              from_declaration_id="D-U1", kind="defer",
                                              target_flight_id="flight-002", note="赶不上本航次")
        freeze_and_decide(self.service, self.clock)
        decisions = decisions_by_declaration(self.service)
        self.assertEqual("deferred", decisions["D-U1"]["status"])
        explain = self.service.explain_declaration("D-U1")
        defer_relations = [row for row in explain["relations"] if row["kind"] == "defer"]
        self.assertEqual(2, len(defer_relations))
        self.assertEqual([1, 2], sorted(row["relation_version"] for row in defer_relations))
        carried_id = next(row["to_declaration_id"] for row in defer_relations
                          if row["to_declaration_id"])
        carried = self.service.explain_declaration(carried_id)["declaration"]
        self.assertEqual("flight-002", carried["flight_id"])
        self.assertEqual("submitted", carried["status"])
        self.assertEqual(10, carried["quantity"])

    def test_frozen_flight_rejects_new_declaration(self):
        self._submit("v1", "rev-1", "D-V1", "fuel-filter", 5, 3, "FF-1")
        self.clock.advance(seconds=17 * 3600)
        self.service.freeze_flight(request_id="fx-freeze", actor_id="admin-001", flight_id="flight-001")
        with self.assertRaises(ConflictError):
            self._submit("v2", "rev-1", "D-V2", "fuel-filter", 5, 3, "FF-1")

    def test_withdraw_only_before_freeze(self):
        self._submit("w1", "rev-1", "D-W1", "fuel-filter", 5, 3, "FF-1")
        self.service.withdraw_declaration(request_id="wd1", actor_id="rev-1", declaration_id="D-W1")
        explain = self.service.explain_declaration("D-W1")
        self.assertEqual("withdrawn", explain["declaration"]["status"])
        self._submit("w2", "rev-1", "D-W2", "fuel-filter", 5, 3, "FF-1")
        self.clock.advance(seconds=17 * 3600)
        self.service.freeze_flight(request_id="fx-freeze", actor_id="admin-001", flight_id="flight-001")
        with self.assertRaises(ConflictError):
            self.service.withdraw_declaration(request_id="wd2", actor_id="rev-1", declaration_id="D-W2")

    def test_reviewer_cannot_touch_other_institution(self):
        self._submit("x1", "rev-1", "D-X1", "fuel-filter", 5, 3, "FF-1")
        with self.assertRaises(PermissionDenied):
            self.service.withdraw_declaration(request_id="wd-x", actor_id="rev-2", declaration_id="D-X1")
        with self.assertRaises(PermissionDenied):
            self.service.add_declaration_relation(request_id="rel-x", actor_id="rev-2",
                                                  from_declaration_id="D-X1", kind="defer",
                                                  target_flight_id="flight-002")

    def test_request_replay_returns_same_receipt(self):
        first = self._submit("r1", "rev-1", "D-R1", "fuel-filter", 5, 3, "FF-1")
        second = self._submit("r1", "rev-1", "D-R1", "fuel-filter", 5, 3, "FF-1")
        self.assertFalse(first.replayed)
        self.assertTrue(second.replayed)
        with self.assertRaises(ConflictError):
            self._submit("r1", "rev-1", "D-R1", "fuel-filter", 6, 3, "FF-1")

    def test_decision_run_is_idempotent_per_freeze(self):
        self._submit("y1", "rev-1", "D-Y1", "fuel-filter", 5, 3, "FF-1")
        self.clock.advance(seconds=17 * 3600)
        self.service.freeze_flight(request_id="fx-freeze", actor_id="admin-001", flight_id="flight-001")
        first = self.service.run_decision(request_id="fx-decide", actor_id="admin-001", flight_id="flight-001")
        replay = self.service.run_decision(request_id="fx-decide", actor_id="admin-001", flight_id="flight-001")
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self.service.run_decision(request_id="fx-decide-again", actor_id="admin-001",
                                      flight_id="flight-001")


class EmergencyRequestTest(unittest.TestCase):
    """截止后紧急需求的双人限时批准。"""

    def setUp(self):
        self.service, self.foundation, self.clock, self.database = new_world()
        self.service.submit_declaration(request_id="e0", actor_id="op-001", declaration_id="D-E0",
                                        flight_id="flight-001", item_id="food-pack", quantity=60,
                                        priority=5)
        freeze_and_decide(self.service, self.clock)

    def tearDown(self):
        self.database.close()

    def _create(self, emergency_id="EM-1", quantity=4, window=600):
        return self.service.create_emergency_request(
            request_id=f"em-create-{emergency_id}", actor_id="rev-1", emergency_id=emergency_id,
            flight_id="flight-001", item_id="o2-bottle", quantity=quantity,
            justification="应急氧气异常消耗", approval_window_seconds=window)

    def test_two_distinct_approvers_required(self):
        self._create()
        with self.assertRaises(PermissionDenied):
            self.service.approve_emergency(request_id="em-self", actor_id="rev-1", emergency_id="EM-1")
        self.service.approve_emergency(request_id="em-a1", actor_id="admin-001", emergency_id="EM-1")
        view = self.service.get_emergency_request("EM-1")
        self.assertEqual("pending", view["emergency"]["status"])
        self.assertEqual(1, len(view["approvals"]))
        with self.assertRaises(ConflictError):
            self.service.approve_emergency(request_id="em-a1-dup", actor_id="admin-001", emergency_id="EM-1")
        replay = self.service.approve_emergency(request_id="em-a1", actor_id="admin-001", emergency_id="EM-1")
        self.assertTrue(replay.replayed)
        self.service.approve_emergency(request_id="em-a2", actor_id="op-001", emergency_id="EM-1")
        view = self.service.get_emergency_request("EM-1")
        self.assertEqual("approved", view["emergency"]["status"])
        self.assertEqual(1, len(view["allocations"]))
        report = self.service.conservation_report("flight-001")
        oxygen = next(row for row in report["reserves"] if row["category"] == "emergency_oxygen")
        self.assertAlmostEqual(20.0, oxygen["emergency_used_weight_kg"])
        self.assertTrue(report["all_ok"])

    def test_approval_window_expires(self):
        self._create(emergency_id="EM-EXP", window=60)
        self.clock.advance(seconds=61)
        with self.assertRaises(ConflictError):
            self.service.approve_emergency(request_id="em-late", actor_id="admin-001", emergency_id="EM-EXP")
        view = self.service.get_emergency_request("EM-EXP")
        self.assertEqual("expired", view["emergency"]["status"])

    def test_emergency_cannot_exceed_reserved_pool(self):
        with self.assertRaises(ValidationError):
            self._create(emergency_id="EM-TOO", quantity=7)

    def test_emergency_requires_frozen_flight(self):
        self.service.register_flight(request_id="fx-flight-3", actor_id="op-001", flight_id="flight-003",
                                     site_id="site-001", code="WIN-03",
                                     cutoff_at="2026-12-02T00:00:00Z", arrival_at="2027-01-01T00:00:00Z")
        with self.assertRaises(ConflictError):
            self.service.create_emergency_request(
                request_id="em-early", actor_id="rev-1", emergency_id="EM-EARLY",
                flight_id="flight-003", item_id="o2-bottle", quantity=1,
                justification="尚未截止", approval_window_seconds=600)


class LoadingAndReallocationTest(unittest.TestCase):
    """装载确认、封舱与稳定转配。"""

    def setUp(self):
        self.service, self.foundation, self.clock, self.database = new_world()
        self.service.submit_declaration(request_id="l-food", actor_id="op-001", declaration_id="D-LFOOD",
                                        flight_id="flight-001", item_id="food-pack", quantity=60, priority=5)
        self.service.submit_declaration(request_id="l-hi", actor_id="rev-1", declaration_id="D-LHI",
                                        flight_id="flight-001", item_id="fuel-filter", quantity=100,
                                        priority=4, batch_id="FF-1")
        self.service.submit_declaration(request_id="l-lo", actor_id="rev-2", declaration_id="D-LLO",
                                        flight_id="flight-001", item_id="fuel-filter", quantity=100,
                                        priority=2, batch_id="FF-1")
        freeze_and_decide(self.service, self.clock)

    def tearDown(self):
        self.database.close()

    def _allocation_of(self, declaration_id):
        allocations = self.service.explain_declaration(declaration_id)["allocations"]
        active = [row for row in allocations if row["status"] != "released"]
        self.assertTrue(active)
        return active[0]

    def test_confirm_load_replay_and_version_conflict(self):
        allocation = self._allocation_of("D-LHI")
        first = self.service.confirm_load(request_id="cf-1", actor_id="op-001",
                                          allocation_id=allocation["allocation_id"], expected_version=1)
        replay = self.service.confirm_load(request_id="cf-1", actor_id="op-001",
                                           allocation_id=allocation["allocation_id"], expected_version=1)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        with self.assertRaises(ConflictError):
            self.service.confirm_load(request_id="cf-2", actor_id="op-001",
                                      allocation_id=allocation["allocation_id"], expected_version=1)
        flight = self.service.get_flight("flight-001")["flight"]
        self.assertEqual("loading", flight["status"])

    def test_seal_requires_all_confirmations(self):
        with self.assertRaises(ConflictError):
            self.service.seal_flight(request_id="seal-early", actor_id="admin-001", flight_id="flight-001")
        pending = self.service.pending_load_confirmations("flight-001")
        for allocation in pending["pending"]:
            self.service.confirm_load(request_id=f"cf-{allocation['allocation_id'][:8]}",
                                      actor_id="op-001", allocation_id=allocation["allocation_id"],
                                      expected_version=allocation["version"])
        receipt = self.service.seal_flight(request_id="seal-ok", actor_id="admin-001", flight_id="flight-001")
        self.assertFalse(receipt.replayed)
        self.assertEqual("closed", self.service.get_flight("flight-001")["flight"]["status"])

    def test_partial_arrival_releases_lowest_priority_first(self):
        self.service.reallocate(request_id="re-pa", actor_id="admin-001", flight_id="flight-001",
                                trigger="partial_arrival",
                                details={"batch_id": "FF-1", "arrived_quantity": 150})
        decisions = decisions_by_declaration(self.service)
        self.assertEqual("approved", decisions["D-LHI"]["status"])
        self.assertEqual(100, decisions["D-LHI"]["approved_quantity"])
        self.assertEqual("partially_approved", decisions["D-LLO"]["status"])
        self.assertEqual(50, decisions["D-LLO"]["approved_quantity"])
        self.assertTrue(self.service.conservation_report("flight-001")["all_ok"])

    def test_loaded_and_claimed_shares_survive_cancellation(self):
        allocation = self._allocation_of("D-LHI")
        self.service.confirm_load(request_id="cf-keep", actor_id="op-001",
                                  allocation_id=allocation["allocation_id"], expected_version=1)
        self.service.reallocate(request_id="re-cancel", actor_id="admin-001", flight_id="flight-001",
                                trigger="flight_cancelled", details={})
        kept = self.service.explain_declaration("D-LHI")["allocations"][0]
        self.assertEqual("loaded", kept["status"])
        released = self.service.explain_declaration("D-LLO")["allocations"][0]
        self.assertEqual("released", released["status"])
        self.assertEqual("cancelled", self.service.get_flight("flight-001")["flight"]["status"])
        self.assertTrue(self.service.conservation_report("flight-001")["all_ok"])

    def test_cancellation_defers_to_target_flight(self):
        self.service.register_flight(request_id="fx-flight-4", actor_id="op-001", flight_id="flight-004",
                                     site_id="site-001", code="WIN-04",
                                     cutoff_at="2026-11-02T00:00:00Z", arrival_at="2026-12-01T00:00:00Z")
        self.service.add_declaration_relation(request_id="rel-defer-2", actor_id="rev-1",
                                              from_declaration_id="D-LHI", kind="defer",
                                              target_flight_id="flight-004")
        self.service.reallocate(request_id="re-cancel-2", actor_id="admin-001", flight_id="flight-001",
                                trigger="flight_cancelled", details={})
        decisions = decisions_by_declaration(self.service)
        self.assertEqual("deferred", decisions["D-LHI"]["status"])
        explain = self.service.explain_declaration("D-LHI")
        carried = next(row for row in explain["relations"]
                       if row["kind"] == "defer" and row["to_declaration_id"])
        carried_declaration = self.service.explain_declaration(carried["to_declaration_id"])["declaration"]
        self.assertEqual("flight-004", carried_declaration["flight_id"])
        self.assertEqual("submitted", carried_declaration["status"])

    def test_batch_expired_reallocates_to_substitute(self):
        # 独立世界：主申报占满低温舱后失效，候补的替代品按冻结规则补上
        service, foundation, clock, database = new_world()
        self.addCleanup(database.close)
        service.submit_declaration(request_id="bx-main", actor_id="rev-1", declaration_id="D-BMAIN",
                                   flight_id="flight-001", item_id="cryo-battery", quantity=100,
                                   priority=4, batch_id="CB-NEW")
        service.submit_declaration(request_id="bx-alt", actor_id="rev-1", declaration_id="D-BALT",
                                   flight_id="flight-001", item_id="cryo-battery-v2", quantity=20,
                                   priority=3)
        service.add_declaration_relation(request_id="bx-rel", actor_id="rev-1",
                                         from_declaration_id="D-BALT", kind="substitute",
                                         to_declaration_id="D-BMAIN", note="二型可顶替一型")
        freeze_and_decide(service, clock)
        decisions = decisions_by_declaration(service)
        self.assertEqual("approved", decisions["D-BMAIN"]["status"])
        self.assertEqual("superseded", decisions["D-BALT"]["status"])
        service.reallocate(request_id="bx-realloc", actor_id="admin-001", flight_id="flight-001",
                           trigger="batch_expired", details={"batch_id": "CB-NEW"})
        decisions = decisions_by_declaration(service)
        self.assertEqual("waitlisted", decisions["D-BMAIN"]["status"])
        self.assertEqual("batch_expired", decisions["D-BMAIN"]["reason_code"])
        self.assertEqual("approved", decisions["D-BALT"]["status"])
        self.assertEqual(20, decisions["D-BALT"]["approved_quantity"])
        self.assertEqual("substitute_fulfilled", decisions["D-BALT"]["reason_code"])
        self.assertTrue(service.conservation_report("flight-001")["all_ok"])

    def test_hold_swap_moves_allocations(self):
        # 独立世界：第三个舱位接受低温物资，换装只移动未装载份额
        service, foundation, clock, database = new_world()
        self.addCleanup(database.close)
        service.add_cargo_hold(request_id="sw-hold-c", actor_id="op-001", flight_id="flight-001",
                               hold_id="hold-C", code="HC", weight_capacity_kg=200.0,
                               volume_capacity_m3=1.0, hazard_levels=["general", "cryogenic"])
        service.submit_declaration(request_id="sw-cryo", actor_id="rev-1", declaration_id="D-SWAP",
                                   flight_id="flight-001", item_id="cryo-battery", quantity=30,
                                   priority=4, batch_id="CB-NEW")
        freeze_and_decide(service, clock)
        allocation = service.explain_declaration("D-SWAP")["allocations"][0]
        self.assertEqual("hold-B", allocation["hold_id"])
        with self.assertRaises(ConflictError):
            service.reallocate(request_id="sw-bad", actor_id="admin-001", flight_id="flight-001",
                               trigger="hold_swap",
                               details={"from_hold_id": "hold-B", "to_hold_id": "hold-A"})
        service.reallocate(request_id="sw-ok", actor_id="admin-001", flight_id="flight-001",
                           trigger="hold_swap",
                           details={"from_hold_id": "hold-B", "to_hold_id": "hold-C"})
        moved = service.explain_declaration("D-SWAP")["allocations"][0]
        self.assertEqual("hold-C", moved["hold_id"])
        self.assertEqual("allocated", moved["status"])
        self.assertTrue(service.conservation_report("flight-001")["all_ok"])

    def test_conservation_report_structure(self):
        report = self.service.conservation_report("flight-001")
        self.assertTrue(report["all_ok"])
        self.assertEqual(2, len(report["holds"]))
        for hold in report["holds"]:
            self.assertGreaterEqual(hold["remaining_weight_kg"], -1e-6)
            self.assertGreaterEqual(hold["remaining_volume_m3"], -1e-6)
        food = next(row for row in report["reserves"] if row["category"] == "food")
        self.assertTrue(food["minimum_satisfied"])

    def test_explain_declaration_shows_reason_trail(self):
        explain = self.service.explain_declaration("D-LLO")
        self.assertEqual("approved", explain["decisions"][0]["status"])
        self.assertTrue(explain["audit"])
        actions = {row["action"] for row in explain["audit"]}
        self.assertIn("logistics.decision.recorded", actions)


if __name__ == "__main__":
    unittest.main()
