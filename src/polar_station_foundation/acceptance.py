"""运行基础登记与越冬物资配额决策的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cargo import CargoService
from .clock import FixedClock
from .errors import ConflictError
from .service import DomainService
from .storage import Database


def run() -> dict[str, object]:
    """执行完整登记链和越冬物资决策链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "acceptance.sqlite3")
        base = datetime(2026, 10, 1, tzinfo=timezone.utc)
        service = DomainService(database, FixedClock(base))

        # -------------------------------------------------- 基础登记链
        service.register_organization(request_id="req-org", actor_id="bootstrap",
                                      organization_id="org-001", name="示范科考机构")
        service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                               display_name="系统管理员", role="admin", organization_id="org-001")
        service.register_actor(request_id="req-operator", actor_id="admin-001", new_actor_id="operator-001",
                               display_name="站务负责人", role="operator", organization_id="org-001")
        service.register_site(request_id="req-site", actor_id="operator-001", site_id="site-001",
                              organization_id="org-001", name="一号科考站点", timezone_name="Asia/Shanghai")
        first = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                           category="station_operator_profile", external_key="record-001",
                                           data={"name": "基础资料", "enabled": True})
        replay = service.record_domain_data(request_id="req-data", actor_id="operator-001", site_id="site-001",
                                            category="station_operator_profile", external_key="record-001",
                                            data={"name": "基础资料", "enabled": True})
        records = service.list_domain_data("site-001")

        # -------------------------------------------------- 越冬物资决策链
        cargo = _run_cargo_chain(database, base)

        valid, event_count = service.verify_audit()
        result = {"status": "ok", "records": len(records), "audit_events": event_count,
                  "audit_valid": valid, "first_replayed": first.replayed,
                  "second_replayed": replay.replayed, **cargo}
        database.close()
        return result


def _run_cargo_chain(database: Database, base: datetime) -> dict[str, object]:
    svc = CargoService(database, FixedClock(base + timedelta(days=11)))
    deadline = (base + timedelta(days=10)).isoformat()
    winter_end = (base + timedelta(days=200)).isoformat()
    expiry_ok = (base + timedelta(days=300)).isoformat()
    expiry_bad = (base + timedelta(days=100)).isoformat()

    svc.register_organization(request_id="c-org-a", actor_id="admin-001",
                              organization_id="org-a", name="科考机构甲")
    svc.register_actor(request_id="c-admin", actor_id="admin-001", new_actor_id="boss",
                       display_name="后勤主管", role="admin", organization_id="org-a")
    svc.register_actor(request_id="c-op2", actor_id="boss", new_actor_id="deputy",
                       display_name="值班负责人", role="operator", organization_id="org-a")
    svc.register_site(request_id="c-site", actor_id="boss", site_id="polar-1",
                      organization_id="org-a", name="越冬站", timezone_name="UTC")

    svc.create_voyage(request_id="c-v1", actor_id="boss", site_id="polar-1", voyage_id="v1",
                      code="FL-001", deadline_at=deadline, winter_end_at=winter_end,
                      dry_weight_capacity=500, dry_volume_capacity=500,
                      hazmat_weight_capacity=300, hazmat_volume_capacity=300,
                      reserved_dry_weight=60, reserved_dry_volume=60,
                      reserved_hazmat_weight=30, reserved_hazmat_volume=30)
    svc.create_voyage(request_id="c-v2", actor_id="boss", site_id="polar-1", voyage_id="v2",
                      code="FL-002", deadline_at=(base + timedelta(days=40)).isoformat(),
                      winter_end_at=winter_end, dry_weight_capacity=500, dry_volume_capacity=500,
                      hazmat_weight_capacity=300, hazmat_volume_capacity=300)

    for mid, code, hz, uw, uv in [
        ("c-m-food", "FOOD", "non_hazardous", 1.0, 1.0),
        ("c-m-o2", "O2", "non_hazardous", 2.0, 2.0),
        ("c-m-batt", "BATT", "lithium_battery", 3.0, 1.0),
        ("c-m-filter", "FILTER", "non_hazardous", 1.0, 1.0),
        ("c-m-fuel", "FUEL", "flammable", 1.0, 2.0),
    ]:
        svc.register_material(request_id=mid, actor_id="boss", site_id="polar-1", material_id=mid,
                              code=code, name=code, hazard_class=hz, unit_weight=uw, unit_volume=uv)

    svc.add_critical_reserve(request_id="c-cr-food", actor_id="boss", voyage_id="v1",
                             material_code="FOOD", quantity=200, unit="箱")
    svc.add_critical_reserve(request_id="c-cr-o2", actor_id="boss", voyage_id="v1",
                             material_code="O2", quantity=50, unit="瓶")
    svc.set_org_quota(request_id="c-q-filter", actor_id="boss", voyage_id="v1",
                      organization_id="org-a", material_code="FILTER", max_quantity=10)

    def submit(rid, code, org, mat, qty, prio, expiry=expiry_ok):
        return svc.submit_application(request_id=rid, actor_id="boss", voyage_id="v1", code=code,
                                      organization_id=org, material_code=mat, quantity=qty,
                                      batch_expiry_at=expiry, priority_score=prio)["application_id"]

    food = submit("c-a-food", "A-FOOD", "org-a", "FOOD", 200, 90)
    o2 = submit("c-a-o2", "B-O2", "org-a", "O2", 60, 80)
    o2_backup = submit("c-a-o2b", "B-O2-BACKUP", "org-a", "O2", 80, 20)
    filt = submit("c-a-filter", "A-FILTER", "org-a", "FILTER", 8, 70)
    filt_split = submit("c-a-filter-part", "A-FILTER-PART", "org-a", "FILTER", 2, 70)
    batt_low = submit("c-a-batt-a", "A-BATT", "org-a", "BATT", 100, 30)
    batt_high = submit("c-a-batt-b", "B-BATT", "org-a", "BATT", 100, 60)
    expired = submit("c-a-fuel-old", "A-FUEL-OLD", "org-a", "FUEL", 10, 95, expiry_bad)

    # 同机构再拆一单试图绕过 10 件的配额上限：必须被拒
    split_order_blocked = False
    try:
        submit("c-a-filter-cheat", "A-FILTER-CHEAT", "org-a", "FILTER", 5, 60)
    except ConflictError:
        split_order_blocked = True

    svc.record_relation(request_id="c-rel-sub", actor_id="boss", voyage_id="v1",
                        from_application_id=batt_low, to_application_id=batt_high,
                        kind="substitute", note="同型号低温电池互为替代")
    svc.record_relation(request_id="c-rel-sub-r", actor_id="boss", voyage_id="v1",
                        from_application_id=batt_high, to_application_id=batt_low,
                        kind="substitute", note="反向替代")
    svc.record_relation(request_id="c-rel-split", actor_id="boss", voyage_id="v1",
                        from_application_id=filt, to_application_id=filt_split, kind="split",
                        quantity=2, note="滤芯拆分配套")
    svc.record_relation(request_id="c-rel-defer-food", actor_id="boss", voyage_id="v1",
                        from_application_id=food, kind="defer", successor_voyage_id="v2",
                        note="若航班取消顺延下一航次")
    svc.record_relation(request_id="c-rel-defer-o2", actor_id="boss", voyage_id="v1",
                        from_application_id=o2_backup, kind="defer", successor_voyage_id="v2")

    frozen = svc.freeze_voyage(request_id="c-freeze", actor_id="boss", voyage_id="v1")
    frozen_replay = svc.freeze_voyage(request_id="c-freeze", actor_id="boss", voyage_id="v1")

    decisions = {}
    for name, aid in [("food", food), ("o2", o2), ("o2_backup", o2_backup),
                      ("batt_low", batt_low), ("batt_high", batt_high), ("expired", expired)]:
        explanation = svc.explain_application("v1", aid)
        decisions[name] = {"status": explanation["application"]["status"],
                           "reasons": [r["code"] for r in explanation["decision_reason"]]}

    # 装载确认与重放
    load1 = svc.confirm_loading(request_id="c-load-food", actor_id="boss", voyage_id="v1",
                                application_id=food, quantity=100)
    load_replay = svc.confirm_loading(request_id="c-load-food", actor_id="boss", voyage_id="v1",
                                      application_id=food, quantity=100)

    # 部分到货：按冻结排序稳定递补
    shortfall = svc.report_shortfall(request_id="c-short", actor_id="boss", voyage_id="v1",
                                     application_id=o2, missing_qty=10, reason="氧气瓶少到10")

    # 截止后紧急需求：双授权限时会签
    emergency = svc.request_emergency_release(request_id="c-emg", actor_id="boss", voyage_id="v1",
                                              organization_id="org-a", material_code="FOOD",
                                              quantity=50, reason="抢修队应急口粮", valid_minutes=60)
    ap1 = svc.approve_emergency_release(request_id="c-ap1", actor_id="boss",
                                        release_id=emergency["release_id"])
    same_person_blocked = False
    try:
        svc.approve_emergency_release(request_id="c-ap-dup", actor_id="boss",
                                      release_id=emergency["release_id"])
    except ConflictError:
        same_person_blocked = True
    ap2 = svc.approve_emergency_release(request_id="c-ap2", actor_id="deputy",
                                        release_id=emergency["release_id"])

    # 物资失效：未装载份额释放并递补
    expiry_event = svc.report_expiry(request_id="c-exp", actor_id="boss", voyage_id="v1",
                                     application_id=filt, quantity=2, reason="两箱滤芯失效")

    # 临时换装：依据已留痕替代关系
    swap = svc.swap_to_substitute(request_id="c-swap", actor_id="boss", voyage_id="v1",
                                  from_application_id=batt_high, to_application_id=batt_low,
                                  quantity=5)

    # 航班取消：只动未装载未领用份额，按顺延/冻结规则稳定转配
    cancellation = svc.report_flight_cancellation(request_id="c-cancel", actor_id="boss",
                                                  voyage_id="v1", reason="极地气旋")

    conservation = svc.verify_conservation("v1")
    return {
        "freeze_hash": frozen["manifest_hash"],
        "freeze_replayed": frozen_replay["replayed"],
        "freeze_hashes_equal": frozen["manifest_hash"] == frozen_replay["manifest_hash"],
        "split_order_blocked": split_order_blocked,
        "decisions": decisions,
        "loading_replayed_without_double_deduct": load_replay["replayed"]
        and load1["loaded_qty"] == 100 and load_replay["loaded_qty"] == 100,
        "shortfall_reassigned": shortfall["reassigned"],
        "emergency_status": [ap1["status"], ap2["status"]],
        "same_person_double_sign_blocked": same_person_blocked,
        "expiry_reassigned": expiry_event["reassigned"],
        "swap_relation_version": swap["relation_version"],
        "cancellation_deferred": cancellation["deferred_count"],
        "cargo_conserved": conservation["conserved"],
        "critical_reserves_met": conservation["critical_reserves_met"],
        "cargo_status": "ok" if _all_good(
            frozen_replay, split_order_blocked, decisions, load1, load_replay,
            ap1, ap2, same_person_blocked, conservation) else "failed",
    }


def _all_good(frozen_replay, split_blocked, decisions, load1, load_replay,
              ap1, ap2, same_blocked, conservation) -> bool:
    return (frozen_replay["replayed"] and split_blocked
            and decisions["food"]["status"] == "approved"
            and decisions["expired"]["status"] == "rejected"
            and decisions["batt_low"]["status"] == "waitlisted"
            and "duplicate_material" in decisions["batt_low"]["reasons"]
            and load_replay["replayed"] and load1["loaded_qty"] == 100
            and ap1["status"] == "awaiting_second_approval"
            and ap2["status"] == "approved" and same_blocked
            and conservation["conserved"] and conservation["critical_reserves_met"])


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] \
        and result["cargo_status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
