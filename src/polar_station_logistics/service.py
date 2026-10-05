"""越冬物资配额与装载决策的领域服务。

在基础服务（组织、操作者、幂等回执、哈希审计链、SQLite 事务）之上实现：

- 航次在截止时刻固化舱位容量、关键储备、批次效期、危险等级与科研优先级快照；
- 申报之间的替代、拆分、顺延关系按版本留痕；
- 机构额度按航次统一核算，拆单不能绕过上限；
- 截止后的紧急需求必须取得两名授权人员的限时批准才能动用预留量；
- 所有状态迁移使用条件更新与请求回执，并发确认或请求重放不会重复扣减；
- 航班取消、部分到货、物资失效、临时换装时只转配尚未装载和领用的份额；
- 决策理由、守恒证明与审计事件可随时查询，系统重启后从 SQLite 接续。
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from polar_station_foundation.audit import append_event, canonical_json, digest
from polar_station_foundation.clock import Clock, SystemClock
from polar_station_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from polar_station_foundation.models import Actor, WriteReceipt
from polar_station_foundation.service import IDENTIFIER
from polar_station_foundation.storage import Database

EPS = 1e-6
INF = float("inf")
HAZARD_LEVELS = frozenset({"general", "flammable", "cryogenic", "oxidizer", "corrosive"})
RELATION_KINDS = frozenset({"substitute", "split", "defer"})
REALLOCATION_TRIGGERS = frozenset({"flight_cancelled", "partial_arrival", "batch_expired", "hold_swap"})
MANAGER_ROLES = frozenset({"admin", "operator"})
APPROVER_ROLES = frozenset({"admin", "operator"})
ACTIVE_ALLOCATION_STATUSES = ("allocated", "loaded", "claimed")
HARD_REJECT_REASONS = frozenset(
    {"batch_expired", "batch_unknown", "hazard_not_allowed", "institution_quota_exceeded", "split_exceeds_parent"}
)
MIN_APPROVAL_WINDOW_SECONDS = 60
MAX_APPROVAL_WINDOW_SECONDS = 7200
PARTIAL_LABELS = {
    "quota": "机构额度",
    "guard": "关键储备护栏",
    "batch": "批次可用量",
    "hold": "舱位余量",
    "flight": "航次总量（扣除预留量）",
}


def _to_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fit(remaining: float, unit: float) -> int:
    """计算在剩余额度内可容纳的整数件数。"""

    if unit <= 0:
        return 0
    return max(0, math.floor((remaining + EPS) / unit))


def _rows(cursor) -> list[dict[str, Any]]:
    return [dict(row) for row in cursor.fetchall()]


class LogisticsService:
    """协调航次、申报、决策、紧急批准、装载确认与转配规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock.now().isoformat().replace("+00:00", "Z")

    def _identifier(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not IDENTIFIER.fullmatch(value):
            raise ValidationError(f"{field} 格式无效")
        return value

    def _text(self, value: str, field: str, limit: int = 200) -> str:
        value = str(value).strip()
        if not value or len(value) > limit:
            raise ValidationError(f"{field} 不能为空且不能超过 {limit} 个字符")
        return value

    def _parse_time(self, value: Any, field: str) -> str:
        try:
            moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValidationError(f"{field} 时间格式无效") from exc
        if moment.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _positive_int(self, value: Any, field: str, maximum: int | None = None) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{field} 必须是正整数")
        if value <= 0 or (maximum is not None and value > maximum):
            raise ValidationError(f"{field} 超出允许范围")
        return value

    def _non_negative_int(self, value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError(f"{field} 必须是非负整数")
        return value

    def _positive_number(self, value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field} 必须是正数")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValidationError(f"{field} 必须是正数")
        return number

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"], row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _require(self, actor: Actor, *roles: str) -> None:
        if actor.role not in roles:
            raise PermissionDenied("当前角色不能执行该动作")

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any], create: Callable[[], tuple[str, str, dict[str, Any]]]) -> WriteReceipt:
        request_id = self._identifier(request_id, "request_id")
        payload_hash = digest(payload)
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["action"] != action or row["payload_hash"] != payload_hash:
                raise ConflictError("request_id 已被不同内容使用")
            return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)
        resource_type, resource_id, response = create()
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, payload_hash, resource_type, resource_id, canonical_json(response), self._now()),
        )
        return WriteReceipt(request_id, resource_type, resource_id, False)

    def _flight_row(self, connection, flight_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM log_flights WHERE flight_id=?", (flight_id,)).fetchone()
        if row is None:
            raise NotFoundError("航次不存在")
        return dict(row)

    def _item_row(self, connection, item_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM log_supply_items WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("物资目录不存在")
        return dict(row)

    def _declaration_row(self, connection, declaration_id: str) -> dict[str, Any]:
        row = connection.execute("SELECT * FROM log_declarations WHERE declaration_id=?", (declaration_id,)).fetchone()
        if row is None:
            raise NotFoundError("申报不存在")
        return dict(row)

    def _site_manager(self, connection, actor: Actor, site_id: str) -> None:
        site = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if site is None:
            raise NotFoundError("场所不存在")
        if actor.role != "admin" and actor.organization_id != site["organization_id"]:
            raise PermissionDenied("不能管理其他组织的场所")

    # ------------------------------------------------------------------
    # 目录、批次与航次建模
    # ------------------------------------------------------------------

    def register_supply_item(self, *, request_id: str, actor_id: str, item_id: str, site_id: str,
                             category: str, name: str, hazard_level: str,
                             unit_weight_kg: Any, unit_volume_m3: Any) -> WriteReceipt:
        payload = {"actor_id": actor_id, "item_id": item_id, "site_id": site_id, "category": category,
                   "name": name, "hazard_level": hazard_level,
                   "unit_weight_kg": unit_weight_kg, "unit_volume_m3": unit_volume_m3}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *MANAGER_ROLES)
            self._site_manager(connection, actor, self._identifier(site_id, "site_id"))
            item_id = self._identifier(item_id, "item_id")
            category = self._identifier(category, "category")
            name = self._text(name, "name")
            if hazard_level not in HAZARD_LEVELS:
                raise ValidationError("hazard_level 不在允许范围内")
            unit_weight = self._positive_number(unit_weight_kg, "unit_weight_kg")
            unit_volume = self._positive_number(unit_volume_m3, "unit_volume_m3")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO log_supply_items(item_id,site_id,category,name,hazard_level,"
                        "unit_weight_kg,unit_volume_m3,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (item_id, site_id, category, name, hazard_level, unit_weight, unit_volume, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("物资目录编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="logistics.item.registered",
                             resource_type="supply_item", resource_id=item_id,
                             detail={"site_id": site_id, "category": category, "hazard_level": hazard_level},
                             occurred_at=self._now())
                return "supply_item", item_id, {"item_id": item_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.register_supply_item", payload=payload, create=create)

    def register_batch(self, *, request_id: str, actor_id: str, batch_id: str, item_id: str,
                       quantity: Any, expires_at: Any) -> WriteReceipt:
        payload = {"actor_id": actor_id, "batch_id": batch_id, "item_id": item_id,
                   "quantity": quantity, "expires_at": expires_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *MANAGER_ROLES)
            item = self._item_row(connection, str(item_id).strip())
            self._site_manager(connection, actor, item["site_id"])
            batch_id = self._identifier(batch_id, "batch_id")
            quantity = self._positive_int(quantity, "quantity")
            expires = self._parse_time(expires_at, "expires_at")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO log_supply_batches(batch_id,item_id,quantity,expires_at,created_at) "
                        "VALUES(?,?,?,?,?)",
                        (batch_id, item["item_id"], quantity, expires, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("批次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="logistics.batch.registered",
                             resource_type="supply_batch", resource_id=batch_id,
                             detail={"item_id": item["item_id"], "quantity": quantity, "expires_at": expires},
                             occurred_at=self._now())
                return "supply_batch", batch_id, {"batch_id": batch_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.register_batch", payload=payload, create=create)

    def register_flight(self, *, request_id: str, actor_id: str, flight_id: str, site_id: str,
                        code: str, cutoff_at: Any, arrival_at: Any) -> WriteReceipt:
        payload = {"actor_id": actor_id, "flight_id": flight_id, "site_id": site_id, "code": code,
                   "cutoff_at": cutoff_at, "arrival_at": arrival_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *MANAGER_ROLES)
            self._site_manager(connection, actor, self._identifier(site_id, "site_id"))
            flight_id = self._identifier(flight_id, "flight_id")
            code = self._identifier(code, "code")
            cutoff = self._parse_time(cutoff_at, "cutoff_at")
            arrival = self._parse_time(arrival_at, "arrival_at")
            if _to_dt(arrival) <= _to_dt(cutoff):
                raise ValidationError("arrival_at 必须晚于 cutoff_at")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO log_flights(flight_id,site_id,code,status,cutoff_at,arrival_at,"
                        "freeze_version,version,created_at) VALUES(?,?,?,?,?,?,0,1,?)",
                        (flight_id, site_id, code, "scheduled", cutoff, arrival, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("航次编号或航次代码已经存在") from exc
                append_event(connection, actor_id=actor_id, action="logistics.flight.registered",
                             resource_type="flight", resource_id=flight_id,
                             detail={"site_id": site_id, "code": code, "cutoff_at": cutoff, "arrival_at": arrival},
                             occurred_at=self._now())
                return "flight", flight_id, {"flight_id": flight_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.register_flight", payload=payload, create=create)

    def add_cargo_hold(self, *, request_id: str, actor_id: str, flight_id: str, hold_id: str,
                       code: str, weight_capacity_kg: Any, volume_capacity_m3: Any,
                       hazard_levels: list[str]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "flight_id": flight_id, "hold_id": hold_id, "code": code,
                   "weight_capacity_kg": weight_capacity_kg, "volume_capacity_m3": volume_capacity_m3,
                   "hazard_levels": hazard_levels}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *MANAGER_ROLES)
            flight = self._flight_row(connection, self._identifier(flight_id, "flight_id"))
            self._site_manager(connection, actor, flight["site_id"])
            hold_id = self._identifier(hold_id, "hold_id")
            code = self._identifier(code, "code")
            weight = self._positive_number(weight_capacity_kg, "weight_capacity_kg")
            volume = self._positive_number(volume_capacity_m3, "volume_capacity_m3")
            if not isinstance(hazard_levels, list) or not hazard_levels:
                raise ValidationError("hazard_levels 必须是非空数组")
            levels = sorted({str(level).strip() for level in hazard_levels})
            if any(level not in HAZARD_LEVELS for level in levels):
                raise ValidationError("hazard_levels 含有不允许的危险等级")

            def create() -> tuple[str, str, dict[str, Any]]:
                if flight["status"] != "scheduled":
                    raise ConflictError("航次已冻结，不能再调整舱位")
                try:
                    connection.execute(
                        "INSERT INTO log_cargo_holds(hold_id,flight_id,code,weight_capacity_kg,volume_capacity_m3,"
                        "hazard_levels_json,version) VALUES(?,?,?,?,?,?,1)",
                        (hold_id, flight["flight_id"], code, weight, volume, canonical_json(levels)),
                    )
                except Exception as exc:
                    raise ConflictError("舱位编号或舱位代码已经存在") from exc
                append_event(connection, actor_id=actor_id, action="logistics.hold.added",
                             resource_type="cargo_hold", resource_id=hold_id,
                             detail={"flight_id": flight["flight_id"], "code": code,
                                     "weight_capacity_kg": weight, "volume_capacity_m3": volume,
                                     "hazard_levels": levels},
                             occurred_at=self._now())
                return "cargo_hold", hold_id, {"hold_id": hold_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.add_cargo_hold", payload=payload, create=create)

    def set_reserve_requirement(self, *, request_id: str, actor_id: str, flight_id: str, category: str,
                                item_id: str, minimum_quantity: Any, reserved_quantity: Any) -> WriteReceipt:
        payload = {"actor_id": actor_id, "flight_id": flight_id, "category": category, "item_id": item_id,
                   "minimum_quantity": minimum_quantity, "reserved_quantity": reserved_quantity}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *MANAGER_ROLES)
            flight = self._flight_row(connection, self._identifier(flight_id, "flight_id"))
            self._site_manager(connection, actor, flight["site_id"])
            category = self._identifier(category, "category")
            item = self._item_row(connection, str(item_id).strip())
            if item["category"] != category:
                raise ValidationError("储备代表物资必须属于对应类别")
            minimum = self._non_negative_int(minimum_quantity, "minimum_quantity")
            reserved = self._non_negative_int(reserved_quantity, "reserved_quantity")
            if minimum == 0 and reserved == 0:
                raise ValidationError("保底数量与预留量不能同时为零")

            def create() -> tuple[str, str, dict[str, Any]]:
                if flight["status"] != "scheduled":
                    raise ConflictError("航次已冻结，不能再调整关键储备")
                existing = connection.execute(
                    "SELECT * FROM log_flight_reserves WHERE flight_id=? AND category=?",
                    (flight["flight_id"], category),
                ).fetchone()
                if existing:
                    if (existing["item_id"] != item["item_id"] or existing["minimum_quantity"] != minimum
                            or existing["reserved_quantity"] != reserved):
                        raise ConflictError("同类别的关键储备已登记为不同内容")
                    return "flight_reserve", f"{flight['flight_id']}:{category}", {"flight_id": flight["flight_id"], "category": category}
                connection.execute(
                    "INSERT INTO log_flight_reserves(flight_id,category,item_id,minimum_quantity,reserved_quantity) "
                    "VALUES(?,?,?,?,?)",
                    (flight["flight_id"], category, item["item_id"], minimum, reserved),
                )
                append_event(connection, actor_id=actor_id, action="logistics.reserve.set",
                             resource_type="flight_reserve", resource_id=f"{flight['flight_id']}:{category}",
                             detail={"flight_id": flight["flight_id"], "category": category,
                                     "item_id": item["item_id"], "minimum_quantity": minimum,
                                     "reserved_quantity": reserved},
                             occurred_at=self._now())
                return "flight_reserve", f"{flight['flight_id']}:{category}", {"flight_id": flight["flight_id"], "category": category}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.set_reserve_requirement", payload=payload, create=create)

    def set_institution_quota(self, *, request_id: str, actor_id: str, flight_id: str, organization_id: str,
                              max_weight_kg: Any, max_volume_m3: Any) -> WriteReceipt:
        payload = {"actor_id": actor_id, "flight_id": flight_id, "organization_id": organization_id,
                   "max_weight_kg": max_weight_kg, "max_volume_m3": max_volume_m3}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *MANAGER_ROLES)
            flight = self._flight_row(connection, self._identifier(flight_id, "flight_id"))
            self._site_manager(connection, actor, flight["site_id"])
            organization_id = self._identifier(organization_id, "organization_id")
            if connection.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                                  (organization_id,)).fetchone() is None:
                raise NotFoundError("组织不存在")
            max_weight = self._positive_number(max_weight_kg, "max_weight_kg")
            max_volume = self._positive_number(max_volume_m3, "max_volume_m3")

            def create() -> tuple[str, str, dict[str, Any]]:
                if flight["status"] != "scheduled":
                    raise ConflictError("航次已冻结，不能再调整机构额度")
                existing = connection.execute(
                    "SELECT * FROM log_institution_quotas WHERE flight_id=? AND organization_id=?",
                    (flight["flight_id"], organization_id),
                ).fetchone()
                resource = f"{flight['flight_id']}:{organization_id}"
                if existing:
                    if (abs(existing["max_weight_kg"] - max_weight) > EPS
                            or abs(existing["max_volume_m3"] - max_volume) > EPS):
                        raise ConflictError("该机构的航次额度已登记为不同内容")
                    return "institution_quota", resource, {"flight_id": flight["flight_id"], "organization_id": organization_id}
                connection.execute(
                    "INSERT INTO log_institution_quotas(flight_id,organization_id,max_weight_kg,max_volume_m3) "
                    "VALUES(?,?,?,?)",
                    (flight["flight_id"], organization_id, max_weight, max_volume),
                )
                append_event(connection, actor_id=actor_id, action="logistics.quota.set",
                             resource_type="institution_quota", resource_id=resource,
                             detail={"flight_id": flight["flight_id"], "organization_id": organization_id,
                                     "max_weight_kg": max_weight, "max_volume_m3": max_volume},
                             occurred_at=self._now())
                return "institution_quota", resource, {"flight_id": flight["flight_id"], "organization_id": organization_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.set_institution_quota", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 申报与申报关系
    # ------------------------------------------------------------------

    def submit_declaration(self, *, request_id: str, actor_id: str, declaration_id: str, flight_id: str,
                           item_id: str, quantity: Any, priority: Any, batch_id: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "declaration_id": declaration_id, "flight_id": flight_id,
                   "item_id": item_id, "quantity": quantity, "priority": priority, "batch_id": batch_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            flight = self._flight_row(connection, self._identifier(flight_id, "flight_id"))
            declaration_id = self._identifier(declaration_id, "declaration_id")
            item = self._item_row(connection, str(item_id).strip())
            if item["site_id"] != flight["site_id"]:
                raise ValidationError("物资目录不属于航次所在场所")
            quantity = self._positive_int(quantity, "quantity")
            priority = self._positive_int(priority, "priority", maximum=5)
            batch_key = None
            if batch_id is not None:
                batch_key = self._identifier(batch_id, "batch_id")
                batch = connection.execute("SELECT * FROM log_supply_batches WHERE batch_id=?",
                                           (batch_key,)).fetchone()
                if batch is None:
                    raise NotFoundError("批次不存在")
                if batch["item_id"] != item["item_id"]:
                    raise ValidationError("批次与物资目录不一致")

            def create() -> tuple[str, str, dict[str, Any]]:
                if flight["status"] != "scheduled":
                    raise ConflictError("航次已冻结，普通申报只能通过紧急需求通道")
                if _to_dt(flight["cutoff_at"]) <= self.clock.now():
                    raise ConflictError("申报已截止")
                try:
                    connection.execute(
                        "INSERT INTO log_declarations(declaration_id,flight_id,organization_id,item_id,batch_id,"
                        "quantity,priority,status,submitted_by,submitted_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
                        (declaration_id, flight["flight_id"], actor.organization_id, item["item_id"], batch_key,
                         quantity, priority, "submitted", actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("申报编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="logistics.declaration.submitted",
                             resource_type="declaration", resource_id=declaration_id,
                             detail={"flight_id": flight["flight_id"], "organization_id": actor.organization_id,
                                     "item_id": item["item_id"], "quantity": quantity, "priority": priority,
                                     "batch_id": batch_key},
                             occurred_at=self._now())
                return "declaration", declaration_id, {"declaration_id": declaration_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.submit_declaration", payload=payload, create=create)

    def add_declaration_relation(self, *, request_id: str, actor_id: str, from_declaration_id: str,
                                 kind: str, to_declaration_id: str | None = None,
                                 target_flight_id: str | None = None, note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "from_declaration_id": from_declaration_id, "kind": kind,
                   "to_declaration_id": to_declaration_id, "target_flight_id": target_flight_id, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            source = self._declaration_row(connection, self._identifier(from_declaration_id, "from_declaration_id"))
            if actor.role == "reviewer" and actor.organization_id != source["organization_id"]:
                raise PermissionDenied("不能为其他机构的申报建立关系")
            if kind not in RELATION_KINDS:
                raise ValidationError("kind 必须是 substitute、split 或 defer")
            note = str(note).strip()[:200]
            target_declaration = None
            target_flight = None
            if kind in ("substitute", "split"):
                if to_declaration_id is None:
                    raise ValidationError("替代与拆分关系必须提供 to_declaration_id")
                target_declaration = self._declaration_row(connection, self._identifier(to_declaration_id, "to_declaration_id"))
                if target_declaration["declaration_id"] == source["declaration_id"]:
                    raise ValidationError("不能和自身建立关系")
            if kind == "defer":
                if target_flight_id is None:
                    raise ValidationError("顺延关系必须提供 target_flight_id")
                target_flight = self._flight_row(connection, self._identifier(target_flight_id, "target_flight_id"))
            relation_id = uuid.uuid4().hex

            def create() -> tuple[str, str, dict[str, Any]]:
                flight = self._flight_row(connection, source["flight_id"])
                if kind in ("substitute", "split"):
                    if flight["status"] != "scheduled":
                        raise ConflictError("替代与拆分关系必须在航次冻结前登记")
                    if source["status"] != "submitted":
                        raise ConflictError("只有待审申报可以建立替代或拆分关系")
                else:
                    if flight["status"] in ("closed", "cancelled"):
                        raise ConflictError("航次已关闭或已取消，不能再登记顺延")
                    if source["status"] not in ("submitted", "waitlisted", "approved", "partially_approved"):
                        raise ConflictError("当前申报状态不能登记顺延")
                if kind == "substitute":
                    if target_declaration["flight_id"] != source["flight_id"]:
                        raise ValidationError("替代关系必须属于同一航次")
                    if target_declaration["status"] != "submitted":
                        raise ConflictError("替代目标必须仍处于待审状态")
                    source_item = self._item_row(connection, source["item_id"])
                    target_item = self._item_row(connection, target_declaration["item_id"])
                    if source_item["category"] != target_item["category"]:
                        raise ValidationError("替代品必须属于同一物资类别")
                if kind == "split":
                    if target_declaration["flight_id"] != source["flight_id"]:
                        raise ValidationError("拆分关系必须属于同一航次")
                    if target_declaration["item_id"] != source["item_id"]:
                        raise ValidationError("拆分部分必须与原始申报同一物资")
                    if target_declaration["organization_id"] != source["organization_id"]:
                        raise ValidationError("拆分部分必须属于同一机构")
                    if target_declaration["status"] != "submitted":
                        raise ConflictError("拆分部分必须仍处于待审状态")
                    parent_edge = connection.execute(
                        "SELECT 1 FROM log_declaration_relations WHERE to_declaration_id=? AND kind='split'",
                        (source["declaration_id"],),
                    ).fetchone()
                    child_edge = connection.execute(
                        "SELECT 1 FROM log_declaration_relations WHERE from_declaration_id=? AND kind='split'",
                        (target_declaration["declaration_id"],),
                    ).fetchone()
                    if parent_edge or child_edge:
                        raise ValidationError("拆分关系不允许嵌套")
                if kind == "defer":
                    if target_flight["flight_id"] == source["flight_id"]:
                        raise ValidationError("顺延目标必须是其他航次")
                    if target_flight["site_id"] != flight["site_id"]:
                        raise ValidationError("顺延目标必须属于同一站点")
                    if target_flight["status"] != "scheduled":
                        raise ConflictError("顺延目标航次已不再接受申报")
                version = connection.execute(
                    "SELECT COALESCE(MAX(relation_version),0)+1 AS next_version FROM log_declaration_relations "
                    "WHERE from_declaration_id=?",
                    (source["declaration_id"],),
                ).fetchone()["next_version"]
                connection.execute(
                    "INSERT INTO log_declaration_relations(relation_id,from_declaration_id,to_declaration_id,kind,"
                    "target_flight_id,note,relation_version,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (relation_id, source["declaration_id"],
                     target_declaration["declaration_id"] if target_declaration else None, kind,
                     target_flight["flight_id"] if target_flight else None, note, version, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="logistics.declaration.relation_added",
                             resource_type="declaration_relation", resource_id=relation_id,
                             detail={"from_declaration_id": source["declaration_id"],
                                     "to_declaration_id": target_declaration["declaration_id"] if target_declaration else None,
                                     "kind": kind,
                                     "target_flight_id": target_flight["flight_id"] if target_flight else None,
                                     "relation_version": version, "note": note},
                             occurred_at=self._now())
                return "declaration_relation", relation_id, {"relation_id": relation_id, "relation_version": version}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.add_declaration_relation", payload=payload, create=create)

    def withdraw_declaration(self, *, request_id: str, actor_id: str, declaration_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "declaration_id": declaration_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            declaration = self._declaration_row(connection, self._identifier(declaration_id, "declaration_id"))
            if actor.role == "reviewer" and actor.organization_id != declaration["organization_id"]:
                raise PermissionDenied("不能撤回其他机构的申报")

            def create() -> tuple[str, str, dict[str, Any]]:
                flight = self._flight_row(connection, declaration["flight_id"])
                if flight["status"] != "scheduled":
                    raise ConflictError("航次已冻结，申报不能再撤回")
                cursor = connection.execute(
                    "UPDATE log_declarations SET status='withdrawn', version=version+1 "
                    "WHERE declaration_id=? AND status='submitted'",
                    (declaration["declaration_id"],),
                )
                if cursor.rowcount == 0:
                    raise ConflictError("申报状态已变化，无法撤回")
                append_event(connection, actor_id=actor_id, action="logistics.declaration.withdrawn",
                             resource_type="declaration", resource_id=declaration["declaration_id"],
                             detail={"flight_id": declaration["flight_id"]}, occurred_at=self._now())
                return "declaration", declaration["declaration_id"], {"declaration_id": declaration["declaration_id"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.withdraw_declaration", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 冻结与装载决策
    # ------------------------------------------------------------------

    def freeze_flight(self, *, request_id: str, actor_id: str, flight_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "flight_id": flight_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *MANAGER_ROLES)
            flight = self._flight_row(connection, self._identifier(flight_id, "flight_id"))
            self._site_manager(connection, actor, flight["site_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                if self.clock.now() < _to_dt(flight["cutoff_at"]):
                    raise ConflictError("尚未到达申报截止时刻")
                cursor = connection.execute(
                    "UPDATE log_flights SET status='frozen', freeze_version=1, version=version+1 "
                    "WHERE flight_id=? AND status='scheduled'",
                    (flight["flight_id"],),
                )
                if cursor.rowcount == 0:
                    raise ConflictError("航次状态不允许冻结")
                snapshot = self._build_snapshot(connection, flight["flight_id"], 1)
                connection.execute(
                    "INSERT INTO log_flight_snapshots(flight_id,freeze_version,snapshot_json,created_at) "
                    "VALUES(?,?,?,?)",
                    (flight["flight_id"], 1, canonical_json(snapshot), self._now()),
                )
                append_event(connection, actor_id=actor_id, action="logistics.flight.frozen",
                             resource_type="flight", resource_id=flight["flight_id"],
                             detail={"freeze_version": 1,
                                     "declarations": len(snapshot["declarations"]),
                                     "holds": len(snapshot["holds"]),
                                     "reserves": len(snapshot["reserves"])},
                             occurred_at=self._now())
                return "flight", flight["flight_id"], {"flight_id": flight["flight_id"], "freeze_version": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.freeze_flight", payload=payload, create=create)

    def _build_snapshot(self, connection, flight_id: str, freeze_version: int) -> dict[str, Any]:
        flight = self._flight_row(connection, flight_id)
        holds = _rows(connection.execute("SELECT * FROM log_cargo_holds WHERE flight_id=? ORDER BY code",
                                         (flight_id,)))
        declarations = _rows(connection.execute(
            "SELECT * FROM log_declarations WHERE flight_id=? ORDER BY submitted_at, declaration_id", (flight_id,)))
        declaration_ids = [row["declaration_id"] for row in declarations]
        relations: list[dict[str, Any]] = []
        if declaration_ids:
            marks = ",".join("?" for _ in declaration_ids)
            relations = _rows(connection.execute(
                f"SELECT * FROM log_declaration_relations WHERE from_declaration_id IN ({marks}) "
                f"OR to_declaration_id IN ({marks}) ORDER BY created_at, relation_id",
                (*declaration_ids, *declaration_ids),
            ))
        return {
            "flight": flight,
            "freeze_version": freeze_version,
            "frozen_at": self._now(),
            "holds": holds,
            "reserves": _rows(connection.execute("SELECT * FROM log_flight_reserves WHERE flight_id=? ORDER BY category",
                                                 (flight_id,))),
            "quotas": _rows(connection.execute("SELECT * FROM log_institution_quotas WHERE flight_id=? "
                                               "ORDER BY organization_id", (flight_id,))),
            "items": _rows(connection.execute("SELECT * FROM log_supply_items WHERE site_id=? ORDER BY item_id",
                                              (flight["site_id"],))),
            "batches": _rows(connection.execute(
                "SELECT b.* FROM log_supply_batches b JOIN log_supply_items i ON b.item_id=i.item_id "
                "WHERE i.site_id=? ORDER BY b.batch_id", (flight["site_id"],))),
            "declarations": declarations,
            "relations": relations,
        }

    def get_snapshot(self, flight_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM log_flight_snapshots WHERE flight_id=? ORDER BY freeze_version DESC LIMIT 1",
            (flight_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("航次尚未冻结，没有快照")
        return json.loads(row["snapshot_json"])

    # ------------------------------------------------------------------
    # 决策引擎
    # ------------------------------------------------------------------

    def _build_state(self, connection, flight: dict[str, Any]) -> dict[str, Any]:
        flight_id = flight["flight_id"]
        holds = []
        for row in connection.execute("SELECT * FROM log_cargo_holds WHERE flight_id=? ORDER BY code",
                                      (flight_id,)):
            holds.append({
                "hold_id": row["hold_id"], "code": row["code"],
                "capacity_w": row["weight_capacity_kg"], "capacity_v": row["volume_capacity_m3"],
                "rem_w": row["weight_capacity_kg"], "rem_v": row["volume_capacity_m3"],
                "hazard_levels": set(json.loads(row["hazard_levels_json"])),
            })
        items = {row["item_id"]: dict(row) for row in connection.execute(
            "SELECT * FROM log_supply_items WHERE site_id=?", (flight["site_id"],))}
        batches = {row["batch_id"]: dict(row) for row in connection.execute(
            "SELECT b.* FROM log_supply_batches b JOIN log_supply_items i ON b.item_id=i.item_id WHERE i.site_id=?",
            (flight["site_id"],))}
        reserves = _rows(connection.execute("SELECT * FROM log_flight_reserves WHERE flight_id=?", (flight_id,)))
        quotas = {row["organization_id"]: dict(row) for row in connection.execute(
            "SELECT * FROM log_institution_quotas WHERE flight_id=?", (flight_id,))}
        active = _rows(connection.execute(
            "SELECT a.*, d.organization_id, d.batch_id FROM log_allocations a "
            "LEFT JOIN log_declarations d ON a.declaration_id=d.declaration_id "
            "WHERE a.flight_id=? AND a.status IN ('allocated','loaded','claimed')", (flight_id,)))
        batch_remaining = {batch_id: row["quantity"] for batch_id, row in batches.items()}
        state = {
            "flight": flight,
            "arrival_at": _to_dt(flight["arrival_at"]),
            "holds": holds,
            "items": items,
            "batches": batches,
            "batch_remaining": batch_remaining,
            "quotas": quotas,
            "org_used_w": {},
            "org_used_v": {},
            "cat_alloc_w": {},
            "cat_alloc_v": {},
            "normal_used_w": 0.0,
            "normal_used_v": 0.0,
            "min_req": {},
            "normal_cap_w": 0.0,
            "normal_cap_v": 0.0,
        }
        for allocation in active:
            hold = next((h for h in holds if h["hold_id"] == allocation["hold_id"]), None)
            if hold is not None:
                hold["rem_w"] -= allocation["weight_kg"]
                hold["rem_v"] -= allocation["volume_m3"]
            category = allocation["category"]
            state["cat_alloc_w"][category] = state["cat_alloc_w"].get(category, 0.0) + allocation["weight_kg"]
            state["cat_alloc_v"][category] = state["cat_alloc_v"].get(category, 0.0) + allocation["volume_m3"]
            if allocation["origin"] != "emergency":
                state["normal_used_w"] += allocation["weight_kg"]
                state["normal_used_v"] += allocation["volume_m3"]
                organization = allocation["organization_id"]
                if organization is not None:
                    state["org_used_w"][organization] = state["org_used_w"].get(organization, 0.0) + allocation["weight_kg"]
                    state["org_used_v"][organization] = state["org_used_v"].get(organization, 0.0) + allocation["volume_m3"]
            if allocation["batch_id"]:
                batch_remaining[allocation["batch_id"]] = batch_remaining.get(allocation["batch_id"], 0) - allocation["quantity"]
        reserved_w = 0.0
        reserved_v = 0.0
        for reserve in reserves:
            item = items[reserve["item_id"]]
            reserved_w += reserve["reserved_quantity"] * item["unit_weight_kg"]
            reserved_v += reserve["reserved_quantity"] * item["unit_volume_m3"]
            state["min_req"][reserve["category"]] = (
                reserve["minimum_quantity"] * item["unit_weight_kg"],
                reserve["minimum_quantity"] * item["unit_volume_m3"],
            )
        total_cap_w = sum(hold["capacity_w"] for hold in holds)
        total_cap_v = sum(hold["capacity_v"] for hold in holds)
        state["normal_cap_w"] = max(0.0, total_cap_w - reserved_w)
        state["normal_cap_v"] = max(0.0, total_cap_v - reserved_v)
        return state

    def _evaluate(self, state: dict[str, Any], declaration: dict[str, Any],
                  quantity: int) -> tuple[int, str, str]:
        """按冻结规则评估一份申报在当前状态下可批准的数量。"""

        item = state["items"][declaration["item_id"]]
        unit_w = item["unit_weight_kg"]
        unit_v = item["unit_volume_m3"]
        fits: dict[str, float] = {}
        batch_id = declaration["batch_id"]
        if batch_id:
            batch = state["batches"].get(batch_id)
            if batch is None:
                return 0, "batch_unknown", "申报引用的批次不存在"
            if _to_dt(batch["expires_at"]) <= state["arrival_at"]:
                return 0, "batch_expired", f"批次 {batch_id} 的效期 {batch['expires_at']} 不晚于航次到达时刻"
            fits["batch"] = state["batch_remaining"].get(batch_id, 0)
        else:
            fits["batch"] = INF
        allowed_holds = [hold for hold in state["holds"] if item["hazard_level"] in hold["hazard_levels"]]
        if not allowed_holds:
            return 0, "hazard_not_allowed", f"没有舱位接受危险等级 {item['hazard_level']}"
        fits["hold"] = sum(min(_fit(hold["rem_w"], unit_w), _fit(hold["rem_v"], unit_v))
                           for hold in allowed_holds)
        quota = state["quotas"].get(declaration["organization_id"])
        if quota is not None:
            used_w = state["org_used_w"].get(declaration["organization_id"], 0.0)
            used_v = state["org_used_v"].get(declaration["organization_id"], 0.0)
            fits["quota"] = min(_fit(quota["max_weight_kg"] - used_w, unit_w),
                                _fit(quota["max_volume_m3"] - used_v, unit_v))
            if fits["quota"] <= 0:
                return 0, "institution_quota_exceeded", "机构额度已用尽，同一机构拆单不能绕过上限"
        else:
            fits["quota"] = INF
        fits["flight"] = min(_fit(state["normal_cap_w"] - state["normal_used_w"], unit_w),
                             _fit(state["normal_cap_v"] - state["normal_used_v"], unit_v))
        category = item["category"]
        if category in state["min_req"]:
            fits["guard"] = INF
        else:
            unmet_w = sum(max(0.0, req[0] - state["cat_alloc_w"].get(cat, 0.0))
                          for cat, req in state["min_req"].items())
            unmet_v = sum(max(0.0, req[1] - state["cat_alloc_v"].get(cat, 0.0))
                          for cat, req in state["min_req"].items())
            guard_w = (state["normal_cap_w"] - state["normal_used_w"]) - unmet_w
            guard_v = (state["normal_cap_v"] - state["normal_used_v"]) - unmet_v
            fits["guard"] = min(_fit(guard_w, unit_w), _fit(guard_v, unit_v))
            if fits["guard"] <= 0:
                return 0, "critical_reserve_guard", "批准将挤占食品与应急氧气等关键储备的保底空间"
        approved = int(min(quantity, fits["batch"], fits["hold"], fits["quota"], fits["flight"], fits["guard"]))
        if approved <= 0:
            if fits["batch"] <= 0:
                return 0, "batch_depleted", "对应批次的可用数量已用尽"
            return 0, "capacity_insufficient", "舱位余量或航次总量不足"
        if approved < quantity:
            binding = min(PARTIAL_LABELS, key=lambda key: fits[key])
            return approved, f"partial_{binding}", (
                f"受{PARTIAL_LABELS[binding]}限制，核定 {approved} 件，剩余 {quantity - approved} 件进入候补"
            )
        return approved, "approved", "全部数量获批"

    def _place(self, connection, state: dict[str, Any], *, item: dict[str, Any], quantity: int,
               declaration_id: str | None, emergency_id: str | None, origin: str,
               organization_id: str | None, batch_id: str | None, actor_id: str) -> list[str]:
        """把核定数量按舱位代码顺序 deterministic 地切分装入舱位。"""

        unit_w = item["unit_weight_kg"]
        unit_v = item["unit_volume_m3"]
        remaining = quantity
        allocation_ids: list[str] = []
        for hold in state["holds"]:
            if remaining <= 0:
                break
            if item["hazard_level"] not in hold["hazard_levels"]:
                continue
            fit = min(remaining, _fit(hold["rem_w"], unit_w), _fit(hold["rem_v"], unit_v))
            if fit <= 0:
                continue
            weight = round(fit * unit_w, 6)
            volume = round(fit * unit_v, 6)
            allocation_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO log_allocations(allocation_id,flight_id,hold_id,declaration_id,emergency_id,category,"
                "origin,quantity,weight_kg,volume_m3,status,version,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (allocation_id, state["flight"]["flight_id"], hold["hold_id"], declaration_id, emergency_id,
                 item["category"], origin, fit, weight, volume, "allocated", 1, self._now(), self._now()),
            )
            hold["rem_w"] -= weight
            hold["rem_v"] -= volume
            allocation_ids.append(allocation_id)
            append_event(connection, actor_id=actor_id, action="logistics.allocation.created",
                         resource_type="allocation", resource_id=allocation_id,
                         detail={"flight_id": state["flight"]["flight_id"], "hold_id": hold["hold_id"],
                                 "declaration_id": declaration_id, "emergency_id": emergency_id,
                                 "origin": origin, "quantity": fit,
                                 "weight_kg": weight, "volume_m3": volume},
                         occurred_at=self._now())
            remaining -= fit
        if remaining > 0:
            raise ConflictError("舱位余量在决策过程中发生变化")
        weight_total = round(quantity * unit_w, 6)
        volume_total = round(quantity * unit_v, 6)
        if origin != "emergency":
            state["normal_used_w"] += weight_total
            state["normal_used_v"] += volume_total
            if organization_id is not None:
                state["org_used_w"][organization_id] = state["org_used_w"].get(organization_id, 0.0) + weight_total
                state["org_used_v"][organization_id] = state["org_used_v"].get(organization_id, 0.0) + volume_total
        category = item["category"]
        state["cat_alloc_w"][category] = state["cat_alloc_w"].get(category, 0.0) + weight_total
        state["cat_alloc_v"][category] = state["cat_alloc_v"].get(category, 0.0) + volume_total
        if batch_id:
            state["batch_remaining"][batch_id] = state["batch_remaining"].get(batch_id, 0) - quantity
        return allocation_ids

    def _record_decision(self, connection, *, run_id: str, flight: dict[str, Any], declaration_id: str,
                         status: str, approved_quantity: int, reason_code: str, reason_detail: str,
                         actor_id: str) -> None:
        decision_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO log_decisions(decision_id,run_id,flight_id,freeze_version,declaration_id,status,"
            "approved_quantity,reason_code,reason_detail,decided_by,decided_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, run_id, flight["flight_id"], flight["freeze_version"], declaration_id, status,
             approved_quantity, reason_code, reason_detail, actor_id, self._now()),
        )
        connection.execute(
            "UPDATE log_declarations SET status=?, version=version+1 WHERE declaration_id=?",
            (status, declaration_id),
        )
        append_event(connection, actor_id=actor_id, action="logistics.decision.recorded",
                     resource_type="declaration", resource_id=declaration_id,
                     detail={"run_id": run_id, "flight_id": flight["flight_id"], "status": status,
                             "approved_quantity": approved_quantity, "reason_code": reason_code,
                             "reason_detail": reason_detail},
                     occurred_at=self._now())

    @staticmethod
    def _decision_status(approved: int, requested: int, reason_code: str) -> str:
        if approved >= requested:
            return "approved"
        if approved > 0:
            return "partially_approved"
        if reason_code in HARD_REJECT_REASONS:
            return "rejected"
        return "waitlisted"

    def _carry_over(self, connection, declaration: dict[str, Any], target_flight_id: str,
                    actor_id: str) -> str | None:
        """为目标航次生成顺延申报并留下关系留痕，返回新申报编号。"""

        target = connection.execute("SELECT * FROM log_flights WHERE flight_id=?",
                                    (target_flight_id,)).fetchone()
        if target is None or target["status"] != "scheduled":
            return None
        existing = connection.execute(
            "SELECT to_declaration_id FROM log_declaration_relations "
            "WHERE from_declaration_id=? AND kind='defer' AND to_declaration_id IS NOT NULL",
            (declaration["declaration_id"],),
        ).fetchone()
        if existing:
            return existing["to_declaration_id"]
        new_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO log_declarations(declaration_id,flight_id,organization_id,item_id,batch_id,quantity,"
            "priority,status,submitted_by,submitted_at,version) VALUES(?,?,?,?,?,?,?,?,?,?,1)",
            (new_id, target_flight_id, declaration["organization_id"], declaration["item_id"],
             declaration["batch_id"], declaration["quantity"], declaration["priority"], "submitted",
             declaration["submitted_by"], self._now()),
        )
        version = connection.execute(
            "SELECT COALESCE(MAX(relation_version),0)+1 AS next_version FROM log_declaration_relations "
            "WHERE from_declaration_id=?",
            (declaration["declaration_id"],),
        ).fetchone()["next_version"]
        relation_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO log_declaration_relations(relation_id,from_declaration_id,to_declaration_id,kind,"
            "target_flight_id,note,relation_version,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (relation_id, declaration["declaration_id"], new_id, "defer", target_flight_id,
             "顺延至目标航次", version, actor_id, self._now()),
        )
        append_event(connection, actor_id=actor_id, action="logistics.declaration.deferred_carryover",
                     resource_type="declaration", resource_id=new_id,
                     detail={"from_declaration_id": declaration["declaration_id"],
                             "target_flight_id": target_flight_id, "relation_id": relation_id},
                     occurred_at=self._now())
        return new_id

    def run_decision(self, *, request_id: str, actor_id: str, flight_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "flight_id": flight_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *MANAGER_ROLES)
            flight = self._flight_row(connection, self._identifier(flight_id, "flight_id"))
            self._site_manager(connection, actor, flight["site_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                if flight["status"] != "frozen":
                    raise ConflictError("航次未处于已冻结状态，不能执行装载决策")
                run_id = uuid.uuid4().hex
                summary = self._execute_decision(connection, flight, run_id, actor_id)
                cursor = connection.execute(
                    "UPDATE log_flights SET status='decided', version=version+1 "
                    "WHERE flight_id=? AND status='frozen'",
                    (flight["flight_id"],),
                )
                if cursor.rowcount == 0:
                    raise ConflictError("航次状态在决策过程中发生变化")
                conservation = self._conservation(connection, flight["flight_id"])
                if not conservation["all_ok"]:
                    raise ConflictError("决策后舱位容量或关键储备不守恒")
                append_event(connection, actor_id=actor_id, action="logistics.decision.completed",
                             resource_type="flight", resource_id=flight["flight_id"],
                             detail={"run_id": run_id, "freeze_version": flight["freeze_version"],
                                     "summary": summary["counts"]},
                             occurred_at=self._now())
                return "decision_run", run_id, {"run_id": run_id, **summary}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.run_decision", payload=payload, create=create)

    def _execute_decision(self, connection, flight: dict[str, Any], run_id: str,
                          actor_id: str) -> dict[str, Any]:
        state = self._build_state(connection, flight)
        declarations = _rows(connection.execute(
            "SELECT * FROM log_declarations WHERE flight_id=? AND status='submitted' "
            "ORDER BY priority DESC, submitted_at ASC, declaration_id ASC",
            (flight["flight_id"],)))
        by_id = {row["declaration_id"]: row for row in declarations}
        relations: list[dict[str, Any]] = []
        if by_id:
            marks = ",".join("?" for _ in by_id)
            relations = _rows(connection.execute(
                f"SELECT * FROM log_declaration_relations WHERE from_declaration_id IN ({marks}) "
                f"OR to_declaration_id IN ({marks})",
                (*by_id, *by_id),
            ))
        defer_map = {rel["from_declaration_id"]: rel for rel in relations if rel["kind"] == "defer"}
        split_children: dict[str, list[str]] = {}
        child_parent: dict[str, str] = {}
        substitute_pairs: list[tuple[str, str]] = []
        for rel in relations:
            if rel["kind"] == "split" and rel["to_declaration_id"] in by_id:
                split_children.setdefault(rel["from_declaration_id"], []).append(rel["to_declaration_id"])
                child_parent[rel["to_declaration_id"]] = rel["from_declaration_id"]
            if rel["kind"] == "substitute":
                substitute_pairs.append((rel["from_declaration_id"], rel["to_declaration_id"]))
        union_parent: dict[str, str] = {}

        def find(node: str) -> str:
            root = node
            while union_parent[root] != root:
                root = union_parent[root]
            while union_parent[node] != root:
                union_parent[node], node = root, union_parent[node]
            return root

        def union(left: str, right: str) -> None:
            for node in (left, right):
                union_parent.setdefault(node, node)
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                union_parent[right_root] = left_root

        for left, right in substitute_pairs:
            if left in by_id and right in by_id:
                union(left, right)
        group_members: dict[str, list[str]] = {}
        for declaration_id in by_id:
            if declaration_id in union_parent:
                group_members.setdefault(find(declaration_id), []).append(declaration_id)

        counts = {"approved": 0, "partially_approved": 0, "waitlisted": 0,
                  "rejected": 0, "superseded": 0, "deferred": 0}
        outcomes: list[dict[str, Any]] = []
        decided: dict[str, str] = {}
        satisfied: dict[str, str] = {}
        child_requested: dict[str, int] = {}

        def record(declaration_id: str, status: str, approved: int, code: str, detail: str) -> None:
            self._record_decision(connection, run_id=run_id, flight=flight, declaration_id=declaration_id,
                                  status=status, approved_quantity=approved, reason_code=code,
                                  reason_detail=detail, actor_id=actor_id)
            counts[status] += 1
            decided[declaration_id] = status
            outcomes.append({"declaration_id": declaration_id, "status": status,
                             "approved_quantity": approved, "reason_code": code})

        for declaration_id, rel in defer_map.items():
            declaration = by_id.get(declaration_id)
            if declaration is None or declaration_id in decided:
                continue
            carried = self._carry_over(connection, declaration, rel["target_flight_id"], actor_id)
            if carried:
                record(declaration_id, "deferred", 0, "deferred_to_flight",
                       f"顺延至航次 {rel['target_flight_id']}，承接申报 {carried}")
            else:
                record(declaration_id, "waitlisted", 0, "defer_target_unavailable",
                       "顺延目标航次不可用，申报转入候补")

        for parent_id, children in split_children.items():
            parent = by_id.get(parent_id)
            if parent is not None and parent_id not in decided:
                record(parent_id, "superseded", 0, "split_into_parts",
                       f"申报被拆分为 {len(children)} 个部分分别核定")

        for declaration in declarations:
            declaration_id = declaration["declaration_id"]
            if declaration_id in decided:
                continue
            group_root = find(declaration_id) if declaration_id in union_parent else None
            if group_root is not None and group_root in satisfied:
                record(declaration_id, "superseded", 0, "replaced_by_substitute",
                       f"同组申报 {satisfied[group_root]} 已获批，本申报被替代")
                continue
            if declaration_id in child_parent:
                parent_id = child_parent[declaration_id]
                parent_quantity = by_id[parent_id]["quantity"] if parent_id in by_id else 0
                if child_requested.get(parent_id, 0) + declaration["quantity"] > parent_quantity:
                    record(declaration_id, "rejected", 0, "split_exceeds_parent",
                           "拆分部分的总量超过原始申报数量")
                    continue
                child_requested[parent_id] = child_requested.get(parent_id, 0) + declaration["quantity"]
            approved, code, detail = self._evaluate(state, declaration, declaration["quantity"])
            status = self._decision_status(approved, declaration["quantity"], code)
            if approved > 0:
                self._place(connection, state, item=state["items"][declaration["item_id"]],
                            quantity=approved, declaration_id=declaration_id, emergency_id=None,
                            origin="decision", organization_id=declaration["organization_id"],
                            batch_id=declaration["batch_id"], actor_id=actor_id)
            record(declaration_id, status, approved, code, detail)
            if approved > 0 and group_root is not None:
                satisfied[group_root] = declaration_id
                for member in group_members.get(group_root, []):
                    if member != declaration_id and decided.get(member) == "waitlisted":
                        record(member, "superseded", 0, "replaced_by_substitute",
                               f"同组申报 {declaration_id} 已获批，候补转为被替代")
        return {"counts": counts, "outcomes": outcomes}

    # ------------------------------------------------------------------
    # 截止后的紧急需求（双人限时批准动用预留量）
    # ------------------------------------------------------------------

    def create_emergency_request(self, *, request_id: str, actor_id: str, emergency_id: str,
                                 flight_id: str, item_id: str, quantity: Any, justification: str,
                                 approval_window_seconds: Any = 1800) -> WriteReceipt:
        payload = {"actor_id": actor_id, "emergency_id": emergency_id, "flight_id": flight_id,
                   "item_id": item_id, "quantity": quantity, "justification": justification,
                   "approval_window_seconds": approval_window_seconds}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            flight = self._flight_row(connection, self._identifier(flight_id, "flight_id"))
            emergency_id = self._identifier(emergency_id, "emergency_id")
            item = self._item_row(connection, str(item_id).strip())
            if item["site_id"] != flight["site_id"]:
                raise ValidationError("物资目录不属于航次所在场所")
            quantity = self._positive_int(quantity, "quantity")
            justification = self._text(justification, "justification", 400)
            window = self._positive_int(approval_window_seconds, "approval_window_seconds",
                                        maximum=MAX_APPROVAL_WINDOW_SECONDS)
            if window < MIN_APPROVAL_WINDOW_SECONDS:
                raise ValidationError("approval_window_seconds 过短")

            def create() -> tuple[str, str, dict[str, Any]]:
                if flight["status"] not in ("frozen", "decided", "loading"):
                    raise ConflictError("只有截止冻结后的航次才能发起紧急需求")
                reserve = connection.execute(
                    "SELECT * FROM log_flight_reserves WHERE flight_id=? AND category=?",
                    (flight["flight_id"], item["category"]),
                ).fetchone()
                if reserve is None or reserve["reserved_quantity"] <= 0:
                    raise ValidationError("该类别没有可动用的预留量")
                used_w, used_v = self._emergency_usage(connection, flight["flight_id"], item["category"])
                reserve_item = self._item_row(connection, reserve["item_id"])
                pool_w = reserve["reserved_quantity"] * reserve_item["unit_weight_kg"]
                pool_v = reserve["reserved_quantity"] * reserve_item["unit_volume_m3"]
                need_w = quantity * item["unit_weight_kg"]
                need_v = quantity * item["unit_volume_m3"]
                if used_w + need_w > pool_w + EPS or used_v + need_v > pool_v + EPS:
                    raise ValidationError("紧急需求超过该类别的预留量")
                expires_at = (self.clock.now().timestamp() + window)
                expires_text = datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                try:
                    connection.execute(
                        "INSERT INTO log_emergency_requests(emergency_id,flight_id,item_id,quantity,justification,"
                        "status,approval_window_seconds,created_by,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (emergency_id, flight["flight_id"], item["item_id"], quantity, justification,
                         "pending", window, actor_id, self._now(), expires_text),
                    )
                except Exception as exc:
                    raise ConflictError("紧急需求编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="logistics.emergency.created",
                             resource_type="emergency_request", resource_id=emergency_id,
                             detail={"flight_id": flight["flight_id"], "item_id": item["item_id"],
                                     "quantity": quantity, "expires_at": expires_text},
                             occurred_at=self._now())
                return "emergency_request", emergency_id, {"emergency_id": emergency_id, "expires_at": expires_text}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.create_emergency_request", payload=payload, create=create)

    def _emergency_usage(self, connection, flight_id: str, category: str) -> tuple[float, float]:
        row = connection.execute(
            "SELECT COALESCE(SUM(weight_kg),0) AS weight, COALESCE(SUM(volume_m3),0) AS volume "
            "FROM log_allocations WHERE flight_id=? AND category=? AND origin='emergency' "
            "AND status IN ('allocated','loaded','claimed')",
            (flight_id, category),
        ).fetchone()
        return row["weight"], row["volume"]

    def _refresh_emergency(self, connection, emergency: dict[str, Any]) -> dict[str, Any]:
        if emergency["status"] == "pending" and self.clock.now() > _to_dt(emergency["expires_at"]):
            connection.execute(
                "UPDATE log_emergency_requests SET status='expired' WHERE emergency_id=? AND status='pending'",
                (emergency["emergency_id"],),
            )
            append_event(connection, actor_id=emergency["created_by"], action="logistics.emergency.expired",
                         resource_type="emergency_request", resource_id=emergency["emergency_id"],
                         detail={"flight_id": emergency["flight_id"]}, occurred_at=self._now())
            emergency = dict(emergency)
            emergency["status"] = "expired"
        return emergency

    def approve_emergency(self, *, request_id: str, actor_id: str, emergency_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "emergency_id": emergency_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *APPROVER_ROLES)
            row = connection.execute("SELECT * FROM log_emergency_requests WHERE emergency_id=?",
                                     (self._identifier(emergency_id, "emergency_id"),)).fetchone()
            if row is None:
                raise NotFoundError("紧急需求不存在")
            emergency = dict(row)

            def create() -> tuple[str, str, dict[str, Any]]:
                emergency = self._refresh_emergency(connection, dict(row))
                if emergency["status"] != "pending":
                    raise ConflictError("紧急需求已终结，不能继续批准")
                if actor_id == emergency["created_by"]:
                    raise PermissionDenied("发起人不能批准自己的紧急需求")
                now = self.clock.now()
                valid_until = datetime.fromtimestamp(
                    now.timestamp() + emergency["approval_window_seconds"], tz=timezone.utc
                ).isoformat().replace("+00:00", "Z")
                try:
                    connection.execute(
                        "INSERT INTO log_emergency_approvals(emergency_id,approver_id,approved_at,valid_until) "
                        "VALUES(?,?,?,?)",
                        (emergency["emergency_id"], actor_id, self._now(), valid_until),
                    )
                except Exception as exc:
                    raise ConflictError("该授权人员已经批准过此紧急需求") from exc
                append_event(connection, actor_id=actor_id, action="logistics.emergency.approval_added",
                             resource_type="emergency_request", resource_id=emergency["emergency_id"],
                             detail={"approver_id": actor_id, "valid_until": valid_until},
                             occurred_at=self._now())
                approvals = _rows(connection.execute(
                    "SELECT * FROM log_emergency_approvals WHERE emergency_id=?",
                    (emergency["emergency_id"],)))
                valid_approvals = [item for item in approvals
                                   if _to_dt(item["valid_until"]) >= now and item["approver_id"] != emergency["created_by"]]
                status = "pending"
                allocation_ids: list[str] = []
                if len(valid_approvals) >= 2:
                    status, allocation_ids = self._finalize_emergency(connection, emergency, actor_id)
                return "emergency_request", emergency["emergency_id"], {
                    "emergency_id": emergency["emergency_id"], "status": status,
                    "approvals": len(valid_approvals), "allocation_ids": allocation_ids,
                }

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.approve_emergency", payload=payload, create=create)

    def _finalize_emergency(self, connection, emergency: dict[str, Any],
                            actor_id: str) -> tuple[str, list[str]]:
        flight = self._flight_row(connection, emergency["flight_id"])
        item = self._item_row(connection, emergency["item_id"])
        state = self._build_state(connection, flight)
        reserve = connection.execute(
            "SELECT * FROM log_flight_reserves WHERE flight_id=? AND category=?",
            (flight["flight_id"], item["category"]),
        ).fetchone()
        reserve_item = self._item_row(connection, reserve["item_id"])
        pool_w = reserve["reserved_quantity"] * reserve_item["unit_weight_kg"]
        pool_v = reserve["reserved_quantity"] * reserve_item["unit_volume_m3"]
        used_w, used_v = self._emergency_usage(connection, flight["flight_id"], item["category"])
        need_w = emergency["quantity"] * item["unit_weight_kg"]
        need_v = emergency["quantity"] * item["unit_volume_m3"]
        allowed_holds = [hold for hold in state["holds"] if item["hazard_level"] in hold["hazard_levels"]]
        hold_fit = sum(min(_fit(hold["rem_w"], item["unit_weight_kg"]),
                           _fit(hold["rem_v"], item["unit_volume_m3"])) for hold in allowed_holds)
        if (used_w + need_w > pool_w + EPS or used_v + need_v > pool_v + EPS
                or hold_fit < emergency["quantity"]):
            connection.execute(
                "UPDATE log_emergency_requests SET status='rejected' WHERE emergency_id=? AND status='pending'",
                (emergency["emergency_id"],),
            )
            append_event(connection, actor_id=actor_id, action="logistics.emergency.finalized",
                         resource_type="emergency_request", resource_id=emergency["emergency_id"],
                         detail={"status": "rejected", "reason": "预留量或舱位余量不足"},
                         occurred_at=self._now())
            return "rejected", []
        allocation_ids = self._place(connection, state, item=item, quantity=emergency["quantity"],
                                     declaration_id=None, emergency_id=emergency["emergency_id"],
                                     origin="emergency", organization_id=None, batch_id=None,
                                     actor_id=actor_id)
        connection.execute(
            "UPDATE log_emergency_requests SET status='approved' WHERE emergency_id=? AND status='pending'",
            (emergency["emergency_id"],),
        )
        append_event(connection, actor_id=actor_id, action="logistics.emergency.finalized",
                     resource_type="emergency_request", resource_id=emergency["emergency_id"],
                     detail={"status": "approved", "allocation_ids": allocation_ids,
                             "category": item["category"]},
                     occurred_at=self._now())
        return "approved", allocation_ids

    def get_emergency_request(self, emergency_id: str) -> dict[str, Any]:
        with self.database.transaction(immediate=True) as connection:
            row = connection.execute("SELECT * FROM log_emergency_requests WHERE emergency_id=?",
                                     (emergency_id,)).fetchone()
            if row is None:
                raise NotFoundError("紧急需求不存在")
            emergency = self._refresh_emergency(connection, dict(row))
            approvals = _rows(connection.execute(
                "SELECT * FROM log_emergency_approvals WHERE emergency_id=? ORDER BY approved_at",
                (emergency_id,)))
            allocations = _rows(connection.execute(
                "SELECT * FROM log_allocations WHERE emergency_id=? ORDER BY created_at", (emergency_id,)))
            return {"emergency": emergency, "approvals": approvals, "allocations": allocations}

    # ------------------------------------------------------------------
    # 装载确认、领用与封舱
    # ------------------------------------------------------------------

    def confirm_load(self, *, request_id: str, actor_id: str, allocation_id: str,
                     expected_version: Any) -> WriteReceipt:
        payload = {"actor_id": actor_id, "allocation_id": allocation_id, "expected_version": expected_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *MANAGER_ROLES)
            row = connection.execute("SELECT * FROM log_allocations WHERE allocation_id=?",
                                     (self._identifier(allocation_id, "allocation_id"),)).fetchone()
            if row is None:
                raise NotFoundError("装载份额不存在")
            allocation = dict(row)
            version = self._positive_int(expected_version, "expected_version")

            def create() -> tuple[str, str, dict[str, Any]]:
                cursor = connection.execute(
                    "UPDATE log_allocations SET status='loaded', version=version+1, updated_at=? "
                    "WHERE allocation_id=? AND status='allocated' AND version=?",
                    (self._now(), allocation["allocation_id"], version),
                )
                if cursor.rowcount == 0:
                    raise ConflictError("装载确认冲突：份额状态或版本已变化，重放请使用原 request_id")
                connection.execute(
                    "UPDATE log_flights SET status='loading', version=version+1 "
                    "WHERE flight_id=? AND status='decided'",
                    (allocation["flight_id"],),
                )
                append_event(connection, actor_id=actor_id, action="logistics.allocation.load_confirmed",
                             resource_type="allocation", resource_id=allocation["allocation_id"],
                             detail={"flight_id": allocation["flight_id"], "hold_id": allocation["hold_id"],
                                     "expected_version": version},
                             occurred_at=self._now())
                return "allocation", allocation["allocation_id"], {
                    "allocation_id": allocation["allocation_id"], "status": "loaded"}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.confirm_load", payload=payload, create=create)

    def claim_allocation(self, *, request_id: str, actor_id: str, allocation_id: str,
                         expected_version: Any) -> WriteReceipt:
        payload = {"actor_id": actor_id, "allocation_id": allocation_id, "expected_version": expected_version}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "reviewer")
            row = connection.execute("SELECT * FROM log_allocations WHERE allocation_id=?",
                                     (self._identifier(allocation_id, "allocation_id"),)).fetchone()
            if row is None:
                raise NotFoundError("装载份额不存在")
            allocation = dict(row)
            version = self._positive_int(expected_version, "expected_version")

            def create() -> tuple[str, str, dict[str, Any]]:
                cursor = connection.execute(
                    "UPDATE log_allocations SET status='claimed', version=version+1, updated_at=? "
                    "WHERE allocation_id=? AND status='loaded' AND version=?",
                    (self._now(), allocation["allocation_id"], version),
                )
                if cursor.rowcount == 0:
                    raise ConflictError("领用确认冲突：份额状态或版本已变化")
                append_event(connection, actor_id=actor_id, action="logistics.allocation.claimed",
                             resource_type="allocation", resource_id=allocation["allocation_id"],
                             detail={"flight_id": allocation["flight_id"]}, occurred_at=self._now())
                return "allocation", allocation["allocation_id"], {
                    "allocation_id": allocation["allocation_id"], "status": "claimed"}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.claim_allocation", payload=payload, create=create)

    def seal_flight(self, *, request_id: str, actor_id: str, flight_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "flight_id": flight_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *MANAGER_ROLES)
            flight = self._flight_row(connection, self._identifier(flight_id, "flight_id"))
            self._site_manager(connection, actor, flight["site_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                if flight["status"] not in ("decided", "loading"):
                    raise ConflictError("航次当前状态不能封舱")
                pending = connection.execute(
                    "SELECT COUNT(*) AS count FROM log_allocations WHERE flight_id=? AND status='allocated'",
                    (flight["flight_id"],),
                ).fetchone()["count"]
                if pending:
                    raise ConflictError("仍存在未确认的装载份额，不能封舱")
                cursor = connection.execute(
                    "UPDATE log_flights SET status='closed', version=version+1 "
                    "WHERE flight_id=? AND status IN ('decided','loading')",
                    (flight["flight_id"],),
                )
                if cursor.rowcount == 0:
                    raise ConflictError("航次状态在封舱过程中发生变化")
                conservation = self._conservation(connection, flight["flight_id"])
                append_event(connection, actor_id=actor_id, action="logistics.flight.sealed",
                             resource_type="flight", resource_id=flight["flight_id"],
                             detail={"conservation_ok": conservation["all_ok"]},
                             occurred_at=self._now())
                return "flight", flight["flight_id"], {
                    "flight_id": flight["flight_id"], "status": "closed",
                    "conservation_ok": conservation["all_ok"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.seal_flight", payload=payload, create=create)

    def pending_load_confirmations(self, flight_id: str) -> dict[str, Any]:
        flight = self._flight_row(self.database.connection, flight_id)
        rows = _rows(self.database.connection.execute(
            "SELECT a.*, h.code AS hold_code, d.item_id AS declaration_item_id, e.item_id AS emergency_item_id "
            "FROM log_allocations a "
            "JOIN log_cargo_holds h ON a.hold_id=h.hold_id "
            "LEFT JOIN log_declarations d ON a.declaration_id=d.declaration_id "
            "LEFT JOIN log_emergency_requests e ON a.emergency_id=e.emergency_id "
            "WHERE a.flight_id=? AND a.status='allocated' ORDER BY a.created_at, a.allocation_id",
            (flight_id,)))
        return {"flight_id": flight_id, "flight_status": flight["status"],
                "pending_count": len(rows), "pending": rows}

    # ------------------------------------------------------------------
    # 稳定转配：航班取消、部分到货、物资失效、临时换装
    # ------------------------------------------------------------------

    def reallocate(self, *, request_id: str, actor_id: str, flight_id: str,
                   trigger: str, details: dict[str, Any]) -> WriteReceipt:
        if not isinstance(details, dict):
            raise ValidationError("details 必须是对象")
        payload = {"actor_id": actor_id, "flight_id": flight_id, "trigger": trigger, "details": details}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *MANAGER_ROLES)
            flight = self._flight_row(connection, self._identifier(flight_id, "flight_id"))
            self._site_manager(connection, actor, flight["site_id"])
            if trigger not in REALLOCATION_TRIGGERS:
                raise ValidationError("trigger 不在允许范围内")

            def create() -> tuple[str, str, dict[str, Any]]:
                run_id = uuid.uuid4().hex
                if trigger == "flight_cancelled":
                    outcomes = self._reallocate_cancelled(connection, flight, run_id, actor_id)
                elif trigger == "partial_arrival":
                    outcomes = self._reallocate_partial_arrival(connection, flight, run_id, actor_id, details)
                elif trigger == "batch_expired":
                    outcomes = self._reallocate_batch_expired(connection, flight, run_id, actor_id, details)
                else:
                    outcomes = self._reallocate_hold_swap(connection, flight, run_id, actor_id, details)
                conservation = self._conservation(connection, flight["flight_id"])
                if not conservation["all_ok"]:
                    raise ConflictError("转配后舱位容量或关键储备不守恒")
                append_event(connection, actor_id=actor_id, action="logistics.reallocation.completed",
                             resource_type="flight", resource_id=flight["flight_id"],
                             detail={"run_id": run_id, "trigger": trigger, "outcomes": outcomes},
                             occurred_at=self._now())
                return "reallocation_run", run_id, {"run_id": run_id, "trigger": trigger, "outcomes": outcomes}

            return self._idempotent(connection, request_id=request_id,
                                    action="logistics.reallocate", payload=payload, create=create)

    def _release_allocation(self, connection, allocation_id: str, actor_id: str, reason: str) -> None:
        cursor = connection.execute(
            "UPDATE log_allocations SET status='released', version=version+1, updated_at=? "
            "WHERE allocation_id=? AND status='allocated'",
            (self._now(), allocation_id),
        )
        if cursor.rowcount == 0:
            raise ConflictError("只有尚未装载的份额可以转配")
        append_event(connection, actor_id=actor_id, action="logistics.allocation.released",
                     resource_type="allocation", resource_id=allocation_id,
                     detail={"reason": reason}, occurred_at=self._now())

    def _released_by_declaration(self, connection, flight_id: str,
                                 declaration_ids: list[str]) -> list[dict[str, Any]]:
        if not declaration_ids:
            return []
        marks = ",".join("?" for _ in declaration_ids)
        return _rows(connection.execute(
            f"SELECT a.*, d.priority, d.submitted_at FROM log_allocations a "
            f"JOIN log_declarations d ON a.declaration_id=d.declaration_id "
            f"WHERE a.flight_id=? AND a.status='allocated' AND a.declaration_id IN ({marks}) "
            f"ORDER BY d.priority ASC, d.submitted_at DESC, d.declaration_id DESC, "
            f"a.hold_id ASC, a.created_at ASC, a.allocation_id ASC",
            (flight_id, *declaration_ids),
        ))

    def _reallocate_cancelled(self, connection, flight: dict[str, Any], run_id: str,
                              actor_id: str) -> list[dict[str, Any]]:
        if flight["status"] in ("closed", "cancelled"):
            raise ConflictError("航次已关闭或已取消")
        releasable = _rows(connection.execute(
            "SELECT * FROM log_allocations WHERE flight_id=? AND status='allocated' ORDER BY allocation_id",
            (flight["flight_id"],)))
        for allocation in releasable:
            self._release_allocation(connection, allocation["allocation_id"], actor_id, "航班取消")
        affected_ids = sorted({row["declaration_id"] for row in releasable if row["declaration_id"]})
        waitlisted = _rows(connection.execute(
            "SELECT declaration_id FROM log_declarations WHERE flight_id=? AND status='waitlisted'",
            (flight["flight_id"],)))
        affected_ids.extend(row["declaration_id"] for row in waitlisted
                            if row["declaration_id"] not in affected_ids)
        outcomes: list[dict[str, Any]] = []
        for declaration_id in affected_ids:
            declaration = self._declaration_row(connection, declaration_id)
            defer = connection.execute(
                "SELECT * FROM log_declaration_relations WHERE from_declaration_id=? AND kind='defer' "
                "ORDER BY relation_version DESC LIMIT 1",
                (declaration_id,),
            ).fetchone()
            carried = self._carry_over(connection, declaration, defer["target_flight_id"], actor_id) if defer else None
            if carried:
                self._record_decision(connection, run_id=run_id, flight=flight, declaration_id=declaration_id,
                                      status="deferred", approved_quantity=0,
                                      reason_code="flight_cancelled_deferred",
                                      reason_detail=f"航班取消，顺延至航次 {defer['target_flight_id']}，承接申报 {carried}",
                                      actor_id=actor_id)
                outcomes.append({"declaration_id": declaration_id, "status": "deferred", "carried_to": carried})
            else:
                self._record_decision(connection, run_id=run_id, flight=flight, declaration_id=declaration_id,
                                      status="waitlisted", approved_quantity=0,
                                      reason_code="flight_cancelled",
                                      reason_detail="航班取消且没有可用的顺延航次，份额转入候补",
                                      actor_id=actor_id)
                outcomes.append({"declaration_id": declaration_id, "status": "waitlisted"})
        pending_emergencies = _rows(connection.execute(
            "SELECT * FROM log_emergency_requests WHERE flight_id=? AND status='pending'",
            (flight["flight_id"],)))
        for emergency in pending_emergencies:
            connection.execute(
                "UPDATE log_emergency_requests SET status='expired' WHERE emergency_id=? AND status='pending'",
                (emergency["emergency_id"],),
            )
        connection.execute(
            "UPDATE log_flights SET status='cancelled', version=version+1 WHERE flight_id=?",
            (flight["flight_id"],),
        )
        append_event(connection, actor_id=actor_id, action="logistics.flight.cancelled",
                     resource_type="flight", resource_id=flight["flight_id"],
                     detail={"released_allocations": len(releasable),
                             "expired_emergencies": len(pending_emergencies)},
                     occurred_at=self._now())
        return outcomes

    def _reallocate_partial_arrival(self, connection, flight: dict[str, Any], run_id: str,
                                    actor_id: str, details: dict[str, Any]) -> list[dict[str, Any]]:
        if flight["status"] not in ("decided", "loading"):
            raise ConflictError("只有完成决策的航次可以登记部分到货")
        batch_id = self._identifier(details.get("batch_id", ""), "batch_id")
        arrived = self._non_negative_int(details.get("arrived_quantity", -1), "arrived_quantity")
        batch = connection.execute("SELECT * FROM log_supply_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None:
            raise NotFoundError("批次不存在")
        if arrived >= batch["quantity"]:
            raise ValidationError("到货数量不小于在册数量，无需转配")
        connection.execute("UPDATE log_supply_batches SET quantity=? WHERE batch_id=?", (arrived, batch_id))
        append_event(connection, actor_id=actor_id, action="logistics.batch.partial_arrival",
                     resource_type="supply_batch", resource_id=batch_id,
                     detail={"flight_id": flight["flight_id"], "arrived_quantity": arrived,
                             "previous_quantity": batch["quantity"]},
                     occurred_at=self._now())
        declaration_rows = _rows(connection.execute(
            "SELECT DISTINCT a.declaration_id FROM log_allocations a "
            "JOIN log_declarations d ON a.declaration_id=d.declaration_id "
            "WHERE a.flight_id=? AND a.status='allocated' AND d.batch_id=?",
            (flight["flight_id"], batch_id)))
        declaration_ids = [row["declaration_id"] for row in declaration_rows]
        allocated_qty = connection.execute(
            "SELECT COALESCE(SUM(a.quantity),0) AS total FROM log_allocations a "
            "JOIN log_declarations d ON a.declaration_id=d.declaration_id "
            "WHERE a.flight_id=? AND a.status='allocated' AND d.batch_id=?",
            (flight["flight_id"], batch_id),
        ).fetchone()["total"]
        shortage = allocated_qty - arrived
        released: dict[str, int] = {}
        if shortage > 0:
            released_total = 0
            for allocation in self._released_by_declaration(connection, flight["flight_id"], declaration_ids):
                if released_total >= shortage:
                    break
                self._release_allocation(connection, allocation["allocation_id"], actor_id, "部分到货")
                released[allocation["declaration_id"]] = released.get(allocation["declaration_id"], 0) + allocation["quantity"]
                released_total += allocation["quantity"]
        return self._re_place_released(connection, flight, run_id, actor_id, released)

    def _reallocate_batch_expired(self, connection, flight: dict[str, Any], run_id: str,
                                  actor_id: str, details: dict[str, Any]) -> list[dict[str, Any]]:
        if flight["status"] not in ("decided", "loading"):
            raise ConflictError("只有完成决策的航次可以登记物资失效")
        batch_id = self._identifier(details.get("batch_id", ""), "batch_id")
        batch = connection.execute("SELECT * FROM log_supply_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if batch is None:
            raise NotFoundError("批次不存在")
        connection.execute("UPDATE log_supply_batches SET expires_at=? WHERE batch_id=?",
                           (self._now(), batch_id))
        append_event(connection, actor_id=actor_id, action="logistics.batch.expired",
                     resource_type="supply_batch", resource_id=batch_id,
                     detail={"flight_id": flight["flight_id"]}, occurred_at=self._now())
        declaration_rows = _rows(connection.execute(
            "SELECT DISTINCT a.declaration_id FROM log_allocations a "
            "JOIN log_declarations d ON a.declaration_id=d.declaration_id "
            "WHERE a.flight_id=? AND a.status='allocated' AND d.batch_id=?",
            (flight["flight_id"], batch_id)))
        released: dict[str, int] = {}
        for allocation in self._released_by_declaration(connection, flight["flight_id"],
                                                        [row["declaration_id"] for row in declaration_rows]):
            self._release_allocation(connection, allocation["allocation_id"], actor_id, "物资失效")
            released[allocation["declaration_id"]] = released.get(allocation["declaration_id"], 0) + allocation["quantity"]
        return self._re_place_released(connection, flight, run_id, actor_id, released)

    def _reallocate_hold_swap(self, connection, flight: dict[str, Any], run_id: str,
                              actor_id: str, details: dict[str, Any]) -> list[dict[str, Any]]:
        if flight["status"] in ("closed", "cancelled"):
            raise ConflictError("航次已关闭或已取消，不能换装")
        from_hold_id = self._identifier(details.get("from_hold_id", ""), "from_hold_id")
        to_hold_id = self._identifier(details.get("to_hold_id", ""), "to_hold_id")
        if from_hold_id == to_hold_id:
            raise ValidationError("源舱位与目标舱位不能相同")
        holds = {row["hold_id"]: dict(row) for row in connection.execute(
            "SELECT * FROM log_cargo_holds WHERE flight_id=?", (flight["flight_id"],))}
        if from_hold_id not in holds or to_hold_id not in holds:
            raise NotFoundError("舱位不存在")
        target_levels = set(json.loads(holds[to_hold_id]["hazard_levels_json"]))
        movable = _rows(connection.execute(
            "SELECT * FROM log_allocations WHERE flight_id=? AND hold_id=? AND status='allocated' "
            "ORDER BY allocation_id",
            (flight["flight_id"], from_hold_id)))
        move_w = 0.0
        move_v = 0.0
        for allocation in movable:
            if allocation["declaration_id"]:
                declaration = self._declaration_row(connection, allocation["declaration_id"])
                item = self._item_row(connection, declaration["item_id"])
            else:
                emergency = connection.execute("SELECT * FROM log_emergency_requests WHERE emergency_id=?",
                                               (allocation["emergency_id"],)).fetchone()
                item = self._item_row(connection, emergency["item_id"])
            if item["hazard_level"] not in target_levels:
                raise ConflictError(f"目标舱位不接受危险等级 {item['hazard_level']}，不能换装")
            move_w += allocation["weight_kg"]
            move_v += allocation["volume_m3"]
        used = connection.execute(
            "SELECT COALESCE(SUM(weight_kg),0) AS weight, COALESCE(SUM(volume_m3),0) AS volume "
            "FROM log_allocations WHERE flight_id=? AND hold_id=? AND status IN ('allocated','loaded','claimed')",
            (flight["flight_id"], to_hold_id),
        ).fetchone()
        if (used["weight"] + move_w > holds[to_hold_id]["weight_capacity_kg"] + EPS
                or used["volume"] + move_v > holds[to_hold_id]["volume_capacity_m3"] + EPS):
            raise ConflictError("目标舱位容量不足，不能换装")
        outcomes = []
        for allocation in movable:
            connection.execute(
                "UPDATE log_allocations SET hold_id=?, version=version+1, updated_at=? "
                "WHERE allocation_id=? AND status='allocated'",
                (to_hold_id, self._now(), allocation["allocation_id"]),
            )
            outcomes.append({"allocation_id": allocation["allocation_id"], "from_hold_id": from_hold_id,
                             "to_hold_id": to_hold_id})
        append_event(connection, actor_id=actor_id, action="logistics.hold.swapped",
                     resource_type="flight", resource_id=flight["flight_id"],
                     detail={"from_hold_id": from_hold_id, "to_hold_id": to_hold_id,
                             "moved_allocations": len(movable)},
                     occurred_at=self._now())
        return outcomes

    def _re_place_released(self, connection, flight: dict[str, Any], run_id: str, actor_id: str,
                           released: dict[str, int]) -> list[dict[str, Any]]:
        """把释放出的未装载份额按冻结规则稳定地重新安置。"""

        outcomes: list[dict[str, Any]] = []
        if not released:
            return outcomes
        state = self._build_state(connection, flight)
        declarations = [self._declaration_row(connection, declaration_id) for declaration_id in released]
        declarations.sort(key=lambda row: (-row["priority"], row["submitted_at"], row["declaration_id"]))
        for declaration in declarations:
            remaining = released[declaration["declaration_id"]]
            approved, code, detail = self._evaluate(state, declaration, remaining)
            if approved > 0:
                self._place(connection, state, item=state["items"][declaration["item_id"]],
                            quantity=approved, declaration_id=declaration["declaration_id"],
                            emergency_id=None, origin="reallocation",
                            organization_id=declaration["organization_id"],
                            batch_id=declaration["batch_id"], actor_id=actor_id)
                status = "approved" if approved == remaining else "partially_approved"
                self._record_decision(connection, run_id=run_id, flight=flight,
                                      declaration_id=declaration["declaration_id"], status=status,
                                      approved_quantity=approved, reason_code=code, reason_detail=detail,
                                      actor_id=actor_id)
                outcomes.append({"declaration_id": declaration["declaration_id"], "status": status,
                                 "approved_quantity": approved})
                remaining -= approved
            if remaining > 0:
                for substitute in self._substitutes_of(connection, declaration):
                    if substitute["status"] not in ("submitted", "waitlisted", "superseded"):
                        continue
                    sub_requested = min(remaining, substitute["quantity"])
                    sub_approved, sub_code, sub_detail = self._evaluate(state, substitute, sub_requested)
                    if sub_approved <= 0:
                        continue
                    self._place(connection, state, item=state["items"][substitute["item_id"]],
                                quantity=sub_approved, declaration_id=substitute["declaration_id"],
                                emergency_id=None, origin="reallocation",
                                organization_id=substitute["organization_id"],
                                batch_id=substitute["batch_id"], actor_id=actor_id)
                    sub_status = "approved" if sub_approved == sub_requested else "partially_approved"
                    self._record_decision(connection, run_id=run_id, flight=flight,
                                          declaration_id=substitute["declaration_id"], status=sub_status,
                                          approved_quantity=sub_approved, reason_code="substitute_fulfilled",
                                          reason_detail=f"替代申报 {declaration['declaration_id']} 的缺口",
                                          actor_id=actor_id)
                    outcomes.append({"declaration_id": substitute["declaration_id"], "status": sub_status,
                                     "approved_quantity": sub_approved, "substitute_for": declaration["declaration_id"]})
                    remaining -= sub_approved
                    if remaining == 0:
                        break
            if remaining > 0 and approved == 0:
                self._record_decision(connection, run_id=run_id, flight=flight,
                                      declaration_id=declaration["declaration_id"], status="waitlisted",
                                      approved_quantity=0, reason_code=code, reason_detail=detail,
                                      actor_id=actor_id)
                outcomes.append({"declaration_id": declaration["declaration_id"], "status": "waitlisted",
                                 "approved_quantity": 0})
        return outcomes

    def _substitutes_of(self, connection, declaration: dict[str, Any]) -> list[dict[str, Any]]:
        return _rows(connection.execute(
            "SELECT d.* FROM log_declaration_relations r "
            "JOIN log_declarations d ON d.declaration_id=r.from_declaration_id "
            "WHERE r.kind='substitute' AND r.to_declaration_id=? "
            "ORDER BY d.priority DESC, d.submitted_at ASC, d.declaration_id ASC",
            (declaration["declaration_id"],),
        ))

    # ------------------------------------------------------------------
    # 查询：守恒证明与决策解释
    # ------------------------------------------------------------------

    def _conservation(self, connection, flight_id: str) -> dict[str, Any]:
        flight = self._flight_row(connection, flight_id)
        holds = []
        holds_ok = True
        for row in connection.execute("SELECT * FROM log_cargo_holds WHERE flight_id=? ORDER BY code",
                                      (flight_id,)):
            used = connection.execute(
                "SELECT COALESCE(SUM(weight_kg),0) AS weight, COALESCE(SUM(volume_m3),0) AS volume "
                "FROM log_allocations WHERE hold_id=? AND status IN ('allocated','loaded','claimed')",
                (row["hold_id"],),
            ).fetchone()
            ok = (used["weight"] <= row["weight_capacity_kg"] + EPS
                  and used["volume"] <= row["volume_capacity_m3"] + EPS)
            holds_ok = holds_ok and ok
            holds.append({
                "hold_id": row["hold_id"], "code": row["code"],
                "capacity_weight_kg": row["weight_capacity_kg"],
                "allocated_weight_kg": round(used["weight"], 6),
                "remaining_weight_kg": round(row["weight_capacity_kg"] - used["weight"], 6),
                "capacity_volume_m3": row["volume_capacity_m3"],
                "allocated_volume_m3": round(used["volume"], 6),
                "remaining_volume_m3": round(row["volume_capacity_m3"] - used["volume"], 6),
                "ok": ok,
            })
        reserves = []
        reserves_ok = True
        minimums_met = True
        for row in connection.execute("SELECT * FROM log_flight_reserves WHERE flight_id=? ORDER BY category",
                                      (flight_id,)):
            item = self._item_row(connection, row["item_id"])
            used_w, used_v = self._emergency_usage(connection, flight_id, row["category"])
            pool_w = row["reserved_quantity"] * item["unit_weight_kg"]
            pool_v = row["reserved_quantity"] * item["unit_volume_m3"]
            allocated = connection.execute(
                "SELECT COALESCE(SUM(weight_kg),0) AS weight FROM log_allocations "
                "WHERE flight_id=? AND category=? AND status IN ('allocated','loaded','claimed')",
                (flight_id, row["category"]),
            ).fetchone()["weight"]
            minimum_w = row["minimum_quantity"] * item["unit_weight_kg"]
            ok = used_w <= pool_w + EPS and used_v <= pool_v + EPS
            satisfied = allocated + EPS >= minimum_w
            reserves_ok = reserves_ok and ok
            minimums_met = minimums_met and satisfied
            reserves.append({
                "category": row["category"], "item_id": row["item_id"],
                "minimum_quantity": row["minimum_quantity"],
                "minimum_weight_kg": round(minimum_w, 6),
                "allocated_weight_kg": round(allocated, 6),
                "minimum_satisfied": satisfied,
                "reserved_quantity": row["reserved_quantity"],
                "reserved_weight_kg": round(pool_w, 6),
                "emergency_used_weight_kg": round(used_w, 6),
                "reserve_remaining_weight_kg": round(pool_w - used_w, 6),
                "ok": ok,
            })
        quotas = []
        quotas_ok = True
        for row in connection.execute("SELECT * FROM log_institution_quotas WHERE flight_id=? "
                                      "ORDER BY organization_id", (flight_id,)):
            used = connection.execute(
                "SELECT COALESCE(SUM(a.weight_kg),0) AS weight, COALESCE(SUM(a.volume_m3),0) AS volume "
                "FROM log_allocations a JOIN log_declarations d ON a.declaration_id=d.declaration_id "
                "WHERE a.flight_id=? AND d.organization_id=? AND a.origin!='emergency' "
                "AND a.status IN ('allocated','loaded','claimed')",
                (flight_id, row["organization_id"]),
            ).fetchone()
            ok = (used["weight"] <= row["max_weight_kg"] + EPS
                  and used["volume"] <= row["max_volume_m3"] + EPS)
            quotas_ok = quotas_ok and ok
            quotas.append({
                "organization_id": row["organization_id"],
                "max_weight_kg": row["max_weight_kg"], "used_weight_kg": round(used["weight"], 6),
                "max_volume_m3": row["max_volume_m3"], "used_volume_m3": round(used["volume"], 6),
                "ok": ok,
            })
        totals = connection.execute(
            "SELECT COALESCE(SUM(weight_kg),0) AS weight, COALESCE(SUM(volume_m3),0) AS volume "
            "FROM log_allocations WHERE flight_id=? AND status IN ('allocated','loaded','claimed')",
            (flight_id,),
        ).fetchone()
        capacity_w = sum(hold["capacity_weight_kg"] for hold in holds)
        capacity_v = sum(hold["capacity_volume_m3"] for hold in holds)
        totals_ok = totals["weight"] <= capacity_w + EPS and totals["volume"] <= capacity_v + EPS
        all_ok = holds_ok and reserves_ok and quotas_ok and totals_ok
        return {
            "flight_id": flight_id,
            "flight_status": flight["status"],
            "holds": holds,
            "reserves": reserves,
            "quotas": quotas,
            "totals": {
                "capacity_weight_kg": round(capacity_w, 6),
                "allocated_weight_kg": round(totals["weight"], 6),
                "capacity_volume_m3": round(capacity_v, 6),
                "allocated_volume_m3": round(totals["volume"], 6),
                "ok": totals_ok,
            },
            "critical_minimums_met": minimums_met,
            "all_ok": all_ok,
        }

    def conservation_report(self, flight_id: str) -> dict[str, Any]:
        report = self._conservation(self.database.connection, flight_id)
        report["generated_at"] = self._now()
        return report

    def explain_declaration(self, declaration_id: str) -> dict[str, Any]:
        connection = self.database.connection
        declaration = self._declaration_row(connection, declaration_id)
        relations = _rows(connection.execute(
            "SELECT * FROM log_declaration_relations WHERE from_declaration_id=? OR to_declaration_id=? "
            "ORDER BY created_at, relation_id",
            (declaration_id, declaration_id)))
        decisions = _rows(connection.execute(
            "SELECT * FROM log_decisions WHERE declaration_id=? ORDER BY rowid", (declaration_id,)))
        allocations = _rows(connection.execute(
            "SELECT * FROM log_allocations WHERE declaration_id=? ORDER BY created_at, allocation_id",
            (declaration_id,)))
        resource_ids = [declaration_id]
        resource_ids.extend(row["relation_id"] for row in relations)
        resource_ids.extend(row["allocation_id"] for row in allocations)
        marks = ",".join("?" for _ in resource_ids)
        audit = _rows(connection.execute(
            f"SELECT * FROM audit_events WHERE resource_id IN ({marks}) ORDER BY sequence",
            resource_ids))
        for row in audit:
            row["detail"] = json.loads(row.pop("detail_json"))
        return {"declaration": declaration, "relations": relations, "decisions": decisions,
                "allocations": allocations, "audit": audit}

    def get_flight(self, flight_id: str) -> dict[str, Any]:
        connection = self.database.connection
        flight = self._flight_row(connection, flight_id)
        holds = _rows(connection.execute("SELECT * FROM log_cargo_holds WHERE flight_id=? ORDER BY code",
                                         (flight_id,)))
        for hold in holds:
            hold["hazard_levels"] = json.loads(hold.pop("hazard_levels_json"))
        reserves = _rows(connection.execute("SELECT * FROM log_flight_reserves WHERE flight_id=? "
                                            "ORDER BY category", (flight_id,)))
        quotas = _rows(connection.execute("SELECT * FROM log_institution_quotas WHERE flight_id=? "
                                          "ORDER BY organization_id", (flight_id,)))
        declaration_counts = _rows(connection.execute(
            "SELECT status, COUNT(*) AS count FROM log_declarations WHERE flight_id=? GROUP BY status",
            (flight_id,)))
        allocation_counts = _rows(connection.execute(
            "SELECT status, COUNT(*) AS count FROM log_allocations WHERE flight_id=? GROUP BY status",
            (flight_id,)))
        return {"flight": flight, "holds": holds, "reserves": reserves, "quotas": quotas,
                "declaration_counts": declaration_counts, "allocation_counts": allocation_counts}

    def list_decisions(self, flight_id: str) -> list[dict[str, Any]]:
        self._flight_row(self.database.connection, flight_id)
        return _rows(self.database.connection.execute(
            "SELECT * FROM log_decisions WHERE flight_id=? ORDER BY rowid", (flight_id,)))
