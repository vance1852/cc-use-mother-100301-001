"""越冬物资配额与装载决策项目的离线端到端验收。

演绎封舱前载荷会审的完整链路：两个课题组重复申报燃料滤芯与低温电池，
截止时刻冻结航次快照，决策引擎保住食品与应急氧气并按机构额度裁剪，
截止后紧急需求经两名授权人员限时批准动用预留量，部分到货触发稳定转配，
系统重启后接续装载确认直至封舱，全程校验容量与关键储备守恒及审计链。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from polar_station_foundation.errors import ConflictError
from polar_station_foundation.service import DomainService

from .service import LogisticsService
from .storage import open_database


class StepClock:
    """可按秒推进的验收时钟。"""

    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value

    def advance(self, seconds: float) -> None:
        self._value += timedelta(seconds=seconds)


def _build_world(service: LogisticsService, foundation: DomainService) -> None:
    foundation.register_organization(request_id="acc-org-station", actor_id="bootstrap",
                                     organization_id="org-station", name="站方后勤")
    foundation.register_organization(request_id="acc-org-lab-1", actor_id="bootstrap",
                                     organization_id="org-lab-1", name="课题组一")
    foundation.register_organization(request_id="acc-org-lab-2", actor_id="bootstrap",
                                     organization_id="org-lab-2", name="课题组二")
    foundation.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin-001",
                              display_name="后勤主管", role="admin", organization_id="org-station")
    foundation.register_actor(request_id="acc-op", actor_id="admin-001", new_actor_id="op-001",
                              display_name="装载调度", role="operator", organization_id="org-station")
    foundation.register_actor(request_id="acc-rev-1", actor_id="admin-001", new_actor_id="rev-1",
                              display_name="课题组一申报员", role="reviewer", organization_id="org-lab-1")
    foundation.register_actor(request_id="acc-rev-2", actor_id="admin-001", new_actor_id="rev-2",
                              display_name="课题组二申报员", role="reviewer", organization_id="org-lab-2")
    foundation.register_site(request_id="acc-site", actor_id="op-001", site_id="site-001",
                             organization_id="org-station", name="极地越冬科考站",
                             timezone_name="Antarctica/Zhongshan")
    service.register_supply_item(request_id="acc-item-food", actor_id="op-001", item_id="food-pack",
                                 site_id="site-001", category="food", name="越冬食品包",
                                 hazard_level="general", unit_weight_kg=2.0, unit_volume_m3=0.02)
    service.register_supply_item(request_id="acc-item-o2", actor_id="op-001", item_id="o2-bottle",
                                 site_id="site-001", category="emergency_oxygen", name="应急氧气瓶",
                                 hazard_level="oxidizer", unit_weight_kg=5.0, unit_volume_m3=0.05)
    service.register_supply_item(request_id="acc-item-filter", actor_id="op-001", item_id="fuel-filter",
                                 site_id="site-001", category="fuel_filter", name="燃料滤芯",
                                 hazard_level="general", unit_weight_kg=1.0, unit_volume_m3=0.005)
    service.register_supply_item(request_id="acc-item-battery", actor_id="op-001", item_id="cryo-battery",
                                 site_id="site-001", category="cryo_battery", name="低温电池",
                                 hazard_level="cryogenic", unit_weight_kg=3.0, unit_volume_m3=0.01)
    service.register_batch(request_id="acc-batch-ff", actor_id="op-001", batch_id="FF-1",
                           item_id="fuel-filter", quantity=1000, expires_at="2027-06-01T00:00:00Z")
    service.register_batch(request_id="acc-batch-cb-old", actor_id="op-001", batch_id="CB-OLD",
                           item_id="cryo-battery", quantity=40, expires_at="2026-10-15T00:00:00Z")
    service.register_batch(request_id="acc-batch-cb-new", actor_id="op-001", batch_id="CB-NEW",
                           item_id="cryo-battery", quantity=100, expires_at="2027-03-01T00:00:00Z")
    service.register_flight(request_id="acc-flight", actor_id="op-001", flight_id="flight-001",
                            site_id="site-001", code="WIN-01",
                            cutoff_at="2026-10-02T00:00:00Z", arrival_at="2026-11-01T00:00:00Z")
    service.add_cargo_hold(request_id="acc-hold-a", actor_id="op-001", flight_id="flight-001",
                           hold_id="hold-A", code="HA", weight_capacity_kg=600.0, volume_capacity_m3=4.5,
                           hazard_levels=["general", "oxidizer"])
    service.add_cargo_hold(request_id="acc-hold-b", actor_id="op-001", flight_id="flight-001",
                           hold_id="hold-B", code="HB", weight_capacity_kg=300.0, volume_capacity_m3=2.0,
                           hazard_levels=["general", "cryogenic", "corrosive"])
    service.set_reserve_requirement(request_id="acc-reserve-food", actor_id="op-001", flight_id="flight-001",
                                    category="food", item_id="food-pack", minimum_quantity=50,
                                    reserved_quantity=10)
    service.set_reserve_requirement(request_id="acc-reserve-o2", actor_id="op-001", flight_id="flight-001",
                                    category="emergency_oxygen", item_id="o2-bottle", minimum_quantity=20,
                                    reserved_quantity=6)
    service.set_institution_quota(request_id="acc-quota-lab-1", actor_id="op-001", flight_id="flight-001",
                                  organization_id="org-lab-1", max_weight_kg=500.0, max_volume_m3=5.0)
    service.set_institution_quota(request_id="acc-quota-lab-2", actor_id="op-001", flight_id="flight-001",
                                  organization_id="org-lab-2", max_weight_kg=120.0, max_volume_m3=1.2)


def _submit_declarations(service: LogisticsService) -> None:
    service.submit_declaration(request_id="acc-d-food", actor_id="op-001", declaration_id="D-FOOD",
                               flight_id="flight-001", item_id="food-pack", quantity=60, priority=5)
    service.submit_declaration(request_id="acc-d-o2", actor_id="op-001", declaration_id="D-O2",
                               flight_id="flight-001", item_id="o2-bottle", quantity=25, priority=5)
    service.submit_declaration(request_id="acc-d-f1", actor_id="rev-1", declaration_id="D-F1",
                               flight_id="flight-001", item_id="fuel-filter", quantity=300,
                               priority=3, batch_id="FF-1")
    service.submit_declaration(request_id="acc-d-c1", actor_id="rev-1", declaration_id="D-C1",
                               flight_id="flight-001", item_id="cryo-battery", quantity=40,
                               priority=4, batch_id="CB-OLD")
    service.submit_declaration(request_id="acc-d-c2", actor_id="rev-2", declaration_id="D-C2",
                               flight_id="flight-001", item_id="cryo-battery", quantity=30,
                               priority=4, batch_id="CB-NEW")
    # 课题组二把 150 件燃料滤芯拆成三单，试图绕过机构额度
    for index, declaration_id in enumerate(("D-F2", "D-F3", "D-F4")):
        service.submit_declaration(request_id=f"acc-d-f{index + 2}", actor_id="rev-2",
                                   declaration_id=declaration_id, flight_id="flight-001",
                                   item_id="fuel-filter", quantity=50, priority=3, batch_id="FF-1")


def run() -> dict[str, object]:
    """执行完整验收链路并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "logistics.sqlite3"
        clock = StepClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        database = open_database(path)
        service = LogisticsService(database, clock)
        foundation = DomainService(database, clock)
        _build_world(service, foundation)
        _submit_declarations(service)

        # 截止时刻固化航次快照
        clock.advance(16 * 3600 + 1)
        service.freeze_flight(request_id="acc-freeze", actor_id="admin-001", flight_id="flight-001")
        snapshot = service.get_snapshot("flight-001")
        service.run_decision(request_id="acc-decide", actor_id="admin-001", flight_id="flight-001")
        decisions = {row["declaration_id"]: row for row in service.list_decisions("flight-001")}
        explain_f3 = service.explain_declaration("D-F3")
        conservation_after_decision = service.conservation_report("flight-001")

        # 截止后紧急需求：两名授权人员限时批准才能动用预留量
        service.create_emergency_request(request_id="acc-em", actor_id="rev-1", emergency_id="EM-1",
                                         flight_id="flight-001", item_id="o2-bottle", quantity=4,
                                         justification="舱内氧烛异常，需补充应急氧气",
                                         approval_window_seconds=1800)
        first_approval = service.approve_emergency(request_id="acc-em-ap-1", actor_id="admin-001",
                                                   emergency_id="EM-1")
        replayed_approval = service.approve_emergency(request_id="acc-em-ap-1", actor_id="admin-001",
                                                      emergency_id="EM-1")
        pending_view = service.get_emergency_request("EM-1")
        service.approve_emergency(request_id="acc-em-ap-2", actor_id="op-001", emergency_id="EM-1")
        emergency_view = service.get_emergency_request("EM-1")

        # 装载确认幂等：重放不重复扣减，过期版本冲突
        food_allocation = service.explain_declaration("D-FOOD")["allocations"][0]
        first_confirm = service.confirm_load(request_id="acc-confirm-food", actor_id="op-001",
                                             allocation_id=food_allocation["allocation_id"],
                                             expected_version=1)
        replayed_confirm = service.confirm_load(request_id="acc-confirm-food", actor_id="op-001",
                                                allocation_id=food_allocation["allocation_id"],
                                                expected_version=1)
        conflict_raised = False
        try:
            service.confirm_load(request_id="acc-confirm-food-2", actor_id="op-001",
                                 allocation_id=food_allocation["allocation_id"], expected_version=1)
        except ConflictError:
            conflict_raised = True
        service.claim_allocation(request_id="acc-claim-food", actor_id="op-001",
                                 allocation_id=food_allocation["allocation_id"], expected_version=2)

        # 部分到货：燃料滤芯批次只到了 250 件，按冻结规则稳定转配未装载份额
        service.reallocate(request_id="acc-realloc", actor_id="admin-001", flight_id="flight-001",
                           trigger="partial_arrival",
                           details={"batch_id": "FF-1", "arrived_quantity": 250})
        conservation_after_reallocation = service.conservation_report("flight-001")
        explain_f1 = service.explain_declaration("D-F1")

        # 系统重启：从 SQLite 接续未完成的装载确认
        database.close()
        database = open_database(path)
        service = LogisticsService(database, clock)
        foundation = DomainService(database, clock)
        pending = service.pending_load_confirmations("flight-001")
        for allocation in pending["pending"]:
            service.confirm_load(request_id=f"acc-resume-{allocation['allocation_id'][:12]}",
                                 actor_id="op-001", allocation_id=allocation["allocation_id"],
                                 expected_version=allocation["version"])
        service.seal_flight(request_id="acc-seal", actor_id="admin-001", flight_id="flight-001")
        final_view = service.get_flight("flight-001")
        conservation_final = service.conservation_report("flight-001")
        audit_valid, audit_events = foundation.verify_audit()
        result = {
            "status": "ok",
            "snapshot_declarations": len(snapshot["declarations"]),
            "decision_status": {key: decisions[key]["status"] for key in
                                ("D-FOOD", "D-O2", "D-C1", "D-C2", "D-F1", "D-F2", "D-F3", "D-F4")},
            "split_reject_reason": explain_f3["decisions"][0]["reason_code"],
            "conservation_after_decision": conservation_after_decision["all_ok"],
            "critical_minimums_met": conservation_after_decision["critical_minimums_met"],
            "emergency_first_approval_replayed": replayed_approval.replayed,
            "emergency_pending_approvals": len(pending_view["approvals"]),
            "emergency_status": emergency_view["emergency"]["status"],
            "emergency_allocations": len(emergency_view["allocations"]),
            "confirm_replayed": replayed_confirm.replayed and not first_confirm.replayed,
            "confirm_conflict_raised": conflict_raised,
            "reallocation_conservation": conservation_after_reallocation["all_ok"],
            "f1_latest_status": explain_f1["decisions"][-1]["status"],
            "f1_latest_approved": explain_f1["decisions"][-1]["approved_quantity"],
            "restart_pending_count": pending["pending_count"],
            "final_flight_status": final_view["flight"]["status"],
            "conservation_final": conservation_final["all_ok"],
            "audit_valid": audit_valid,
            "audit_events": audit_events,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    expected = {"approved", "partially_approved", "waitlisted", "rejected"}
    ok = (result["status"] == "ok"
          and result["audit_valid"]
          and result["conservation_final"]
          and result["final_flight_status"] == "closed"
          and set(result["decision_status"].values()) <= expected)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
