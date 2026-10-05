"""物流决策测试共用的夹具：可推进时钟与标准站区世界。"""

from datetime import datetime, timedelta, timezone

from polar_station_foundation.service import DomainService

from polar_station_logistics.service import LogisticsService
from polar_station_logistics.storage import open_database


class MutableClock:
    """可推进的测试时钟。"""

    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value

    def advance(self, **kwargs) -> None:
        self._value += timedelta(**kwargs)


def build_world(database, clock):
    """登记两个课题组、站方操作者、物资目录、航次、舱位、储备与额度。"""

    foundation = DomainService(database, clock)
    service = LogisticsService(database, clock)
    foundation.register_organization(request_id="fx-org-station", actor_id="bootstrap",
                                     organization_id="org-station", name="站方后勤")
    foundation.register_organization(request_id="fx-org-lab-1", actor_id="bootstrap",
                                     organization_id="org-lab-1", name="课题组一")
    foundation.register_organization(request_id="fx-org-lab-2", actor_id="bootstrap",
                                     organization_id="org-lab-2", name="课题组二")
    foundation.register_actor(request_id="fx-admin", actor_id="bootstrap", new_actor_id="admin-001",
                              display_name="后勤主管", role="admin", organization_id="org-station")
    foundation.register_actor(request_id="fx-op", actor_id="admin-001", new_actor_id="op-001",
                              display_name="装载调度", role="operator", organization_id="org-station")
    foundation.register_actor(request_id="fx-rev-1", actor_id="admin-001", new_actor_id="rev-1",
                              display_name="课题组一申报员", role="reviewer", organization_id="org-lab-1")
    foundation.register_actor(request_id="fx-rev-2", actor_id="admin-001", new_actor_id="rev-2",
                              display_name="课题组二申报员", role="reviewer", organization_id="org-lab-2")
    foundation.register_site(request_id="fx-site", actor_id="op-001", site_id="site-001",
                             organization_id="org-station", name="极地越冬科考站",
                             timezone_name="Antarctica/Zhongshan")
    service.register_supply_item(request_id="fx-item-food", actor_id="op-001", item_id="food-pack",
                                 site_id="site-001", category="food", name="越冬食品包",
                                 hazard_level="general", unit_weight_kg=2.0, unit_volume_m3=0.02)
    service.register_supply_item(request_id="fx-item-o2", actor_id="op-001", item_id="o2-bottle",
                                 site_id="site-001", category="emergency_oxygen", name="应急氧气瓶",
                                 hazard_level="oxidizer", unit_weight_kg=5.0, unit_volume_m3=0.05)
    service.register_supply_item(request_id="fx-item-filter", actor_id="op-001", item_id="fuel-filter",
                                 site_id="site-001", category="fuel_filter", name="燃料滤芯",
                                 hazard_level="general", unit_weight_kg=1.0, unit_volume_m3=0.005)
    service.register_supply_item(request_id="fx-item-battery", actor_id="op-001", item_id="cryo-battery",
                                 site_id="site-001", category="cryo_battery", name="低温电池",
                                 hazard_level="cryogenic", unit_weight_kg=3.0, unit_volume_m3=0.01)
    service.register_supply_item(request_id="fx-item-battery-2", actor_id="op-001", item_id="cryo-battery-v2",
                                 site_id="site-001", category="cryo_battery", name="低温电池二型",
                                 hazard_level="cryogenic", unit_weight_kg=3.0, unit_volume_m3=0.01)
    service.register_batch(request_id="fx-batch-ff", actor_id="op-001", batch_id="FF-1",
                           item_id="fuel-filter", quantity=500, expires_at="2027-06-01T00:00:00Z")
    service.register_batch(request_id="fx-batch-cb-old", actor_id="op-001", batch_id="CB-OLD",
                           item_id="cryo-battery", quantity=40, expires_at="2026-10-15T00:00:00Z")
    service.register_batch(request_id="fx-batch-cb-new", actor_id="op-001", batch_id="CB-NEW",
                           item_id="cryo-battery", quantity=100, expires_at="2027-03-01T00:00:00Z")
    service.register_flight(request_id="fx-flight", actor_id="op-001", flight_id="flight-001",
                            site_id="site-001", code="WIN-01",
                            cutoff_at="2026-10-02T00:00:00Z", arrival_at="2026-11-01T00:00:00Z")
    service.add_cargo_hold(request_id="fx-hold-a", actor_id="op-001", flight_id="flight-001",
                           hold_id="hold-A", code="HA", weight_capacity_kg=600.0, volume_capacity_m3=4.5,
                           hazard_levels=["general", "oxidizer"])
    service.add_cargo_hold(request_id="fx-hold-b", actor_id="op-001", flight_id="flight-001",
                           hold_id="hold-B", code="HB", weight_capacity_kg=300.0, volume_capacity_m3=2.0,
                           hazard_levels=["general", "cryogenic", "corrosive"])
    service.set_reserve_requirement(request_id="fx-reserve-food", actor_id="op-001", flight_id="flight-001",
                                    category="food", item_id="food-pack", minimum_quantity=50,
                                    reserved_quantity=10)
    service.set_reserve_requirement(request_id="fx-reserve-o2", actor_id="op-001", flight_id="flight-001",
                                    category="emergency_oxygen", item_id="o2-bottle", minimum_quantity=20,
                                    reserved_quantity=6)
    service.set_institution_quota(request_id="fx-quota-lab-1", actor_id="op-001", flight_id="flight-001",
                                  organization_id="org-lab-1", max_weight_kg=500.0, max_volume_m3=5.0)
    service.set_institution_quota(request_id="fx-quota-lab-2", actor_id="op-001", flight_id="flight-001",
                                  organization_id="org-lab-2", max_weight_kg=120.0, max_volume_m3=1.2)
    return service, foundation


def freeze_and_decide(service, clock, flight_id="flight-001"):
    """推进到截止时刻之后，冻结并执行装载决策。"""

    clock.advance(seconds=17 * 3600)
    service.freeze_flight(request_id="fx-freeze", actor_id="admin-001", flight_id=flight_id)
    service.run_decision(request_id="fx-decide", actor_id="admin-001", flight_id=flight_id)


def decisions_by_declaration(service, flight_id="flight-001"):
    """按申报编号汇总最新决策。"""

    latest = {}
    for row in service.list_decisions(flight_id):
        latest[row["declaration_id"]] = row
    return latest


def new_world():
    """返回 (service, foundation, clock, database) 四元组。"""

    clock = MutableClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
    database = open_database()
    service, foundation = build_world(database, clock)
    return service, foundation, clock, database
