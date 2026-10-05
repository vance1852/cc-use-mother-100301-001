"""越冬物资配额、冻结决策与装载转配领域服务。"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import (
    ConflictError,
    InfeasibleError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from .service import DomainService

EPS = 1e-9

HAZARD_CLASSES = frozenset({
    "non_hazardous", "flammable", "corrosive", "lithium_battery", "oxidizer", "toxic",
})
AUTHORIZED_EMERGENCY_ROLES = frozenset({"admin", "operator"})
WRITE_ROLES = frozenset({"admin", "operator"})

# 改变配载余额的台账事件；装载与领用只影响在途状态，不改变配额余额。
ALLOCATION_LEDGER_EVENTS = frozenset({
    "allocated", "partial", "waitlisted", "rejected", "released",
    "transferred_in", "transferred_out", "deferred", "emergency_allocated",
    "emergency_released", "expired", "swapped",
})
# 仅用于核算预留池占用的紧急事件（分配为正、退回为负）。
EMERGENCY_LEDGER_EVENTS = ("emergency_allocated", "emergency_released")


def parse_ts(value: str, field: str) -> datetime:
    """解析必须带时区的 ISO 8601 时间。"""

    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} 必须是 ISO 8601 时间")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{field} 时间格式无效") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def compartment_of(hazard_class: str) -> str:
    return "dry" if hazard_class == "non_hazardous" else "hazmat"


def row_dict(row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


class CargoService(DomainService):
    """处理航次、申报、冻结决策、应急预留与稳定转配。"""

    # ------------------------------------------------------------- 幂等预检

    def _peek_replay(self, conn, *, request_id: str, action: str, payload: dict[str, Any]):
        """在状态校验之前返回已处理请求的原始回执，保证重放绝不重复扣减。"""

        request_id = self._identifier(request_id, "request_id")
        row = conn.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action:
            raise ConflictError("request_id 已被其他动作使用")
        if row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return {**json.loads(row["response_json"]), "replayed": True}

    # ------------------------------------------------------------------ 航次

    def create_voyage(self, *, request_id: str, actor_id: str, site_id: str, voyage_id: str,
                      code: str, deadline_at: str, winter_end_at: str,
                      dry_weight_capacity: float, dry_volume_capacity: float,
                      hazmat_weight_capacity: float, hazmat_volume_capacity: float,
                      reserved_dry_weight: float = 0.0, reserved_dry_volume: float = 0.0,
                      reserved_hazmat_weight: float = 0.0,
                      reserved_hazmat_volume: float = 0.0) -> dict[str, Any]:
        payload = locals_extra(
            request_id=request_id, actor_id=actor_id, site_id=site_id, voyage_id=voyage_id, code=code,
            deadline_at=deadline_at, winter_end_at=winter_end_at,
            dry_weight_capacity=dry_weight_capacity, dry_volume_capacity=dry_volume_capacity,
            hazmat_weight_capacity=hazmat_weight_capacity, hazmat_volume_capacity=hazmat_volume_capacity,
            reserved_dry_weight=reserved_dry_weight, reserved_dry_volume=reserved_dry_volume,
            reserved_hazmat_weight=reserved_hazmat_weight,
            reserved_hazmat_volume=reserved_hazmat_volume,
        )
        deadline = parse_ts(deadline_at, "deadline_at")
        winter_end = parse_ts(winter_end_at, "winter_end_at")
        if winter_end <= deadline:
            raise ValidationError("winter_end_at 必须晚于 deadline_at")
        caps = positive_dict(
            dry_weight_capacity=dry_weight_capacity, dry_volume_capacity=dry_volume_capacity,
            hazmat_weight_capacity=hazmat_weight_capacity, hazmat_volume_capacity=hazmat_volume_capacity,
        )
        reserves = {
            "dry_weight": non_negative(reserved_dry_weight, "reserved_dry_weight"),
            "dry_volume": non_negative(reserved_dry_volume, "reserved_dry_volume"),
            "hazmat_weight": non_negative(reserved_hazmat_weight, "reserved_hazmat_weight"),
            "hazmat_volume": non_negative(reserved_hazmat_volume, "reserved_hazmat_volume"),
        }
        if reserves["dry_weight"] > caps["dry_weight_capacity"] + EPS or \
           reserves["dry_volume"] > caps["dry_volume_capacity"] + EPS or \
           reserves["hazmat_weight"] > caps["hazmat_weight_capacity"] + EPS or \
           reserves["hazmat_volume"] > caps["hazmat_volume_capacity"] + EPS:
            raise ValidationError("各舱预留量不能超过该舱容量")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            replay = self._peek_replay(conn, request_id=request_id, action="create_voyage", payload=payload)
            if replay is not None:
                return replay
            voyage_id = self._identifier(voyage_id, "voyage_id")
            code = self._text(code, "code", 80)
            site = conn.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
            if site is None:
                raise NotFoundError("场所不存在")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO voyages(voyage_id,site_id,code,deadline_at,winter_end_at,status,"
                        "dry_weight_capacity,dry_volume_capacity,hazmat_weight_capacity,hazmat_volume_capacity,"
                        "reserved_dry_weight,reserved_dry_volume,reserved_hazmat_weight,reserved_hazmat_volume,"
                        "created_by,created_at) "
                        "VALUES(?,?,?,?,?, 'open', ?,?,?,?,?,?,?,?,?,?)",
                        (voyage_id, site_id, code, deadline.isoformat(), winter_end.isoformat(),
                         caps["dry_weight_capacity"], caps["dry_volume_capacity"],
                         caps["hazmat_weight_capacity"], caps["hazmat_volume_capacity"],
                         reserves["dry_weight"], reserves["dry_volume"],
                         reserves["hazmat_weight"], reserves["hazmat_volume"],
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("航次编号或航次代码已经存在") from exc
                append_event(conn, actor_id=actor_id, action="voyage.created", resource_type="voyage",
                             resource_id=voyage_id, detail={"code": code, "deadline_at": deadline.isoformat()},
                             occurred_at=self._now())
                return "voyage", voyage_id, {"voyage_id": voyage_id, "status": "open"}

            receipt = self._idempotent(conn, request_id=request_id, action="create_voyage",
                                       payload=payload, create=create)
            return {"voyage_id": voyage_id, **receipt.__dict__}

    def get_voyage(self, voyage_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM voyages WHERE voyage_id=?", (voyage_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("航次不存在")
        result = row_dict(row)
        snap = self.database.connection.execute(
            "SELECT manifest_hash FROM freeze_snapshots WHERE voyage_id=? ORDER BY freeze_version DESC LIMIT 1",
            (voyage_id,),
        ).fetchone()
        result["manifest_hash"] = snap["manifest_hash"] if snap else None
        return result

    # ------------------------------------------------------------------ 物料

    def register_material(self, *, request_id: str, actor_id: str, site_id: str, material_id: str,
                          code: str, name: str, hazard_class: str,
                          unit_weight: float, unit_volume: float) -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, site_id=site_id,
                               material_id=material_id, code=code, name=name, hazard_class=hazard_class,
                               unit_weight=unit_weight, unit_volume=unit_volume)
        if hazard_class not in HAZARD_CLASSES:
            raise ValidationError("hazard_class 不在允许范围内")
        unit_weight = positive(unit_weight, "unit_weight")
        unit_volume = positive(unit_volume, "unit_volume")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES, "reviewer")
            replay = self._peek_replay(conn, request_id=request_id, action="register_material",
                                       payload=payload)
            if replay is not None:
                return replay
            material_id = self._identifier(material_id, "material_id")
            code = self._identifier(code, "material code")
            name = self._text(name, "name")

            def create():
                try:
                    conn.execute(
                        "INSERT INTO materials(material_id,site_id,code,name,hazard_class,"
                        "unit_weight,unit_volume,active,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,?,1,?,?)",
                        (material_id, site_id, code, name, hazard_class, unit_weight, unit_volume,
                         actor_id, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("物料编号或代码已经存在") from exc
                append_event(conn, actor_id=actor_id, action="material.registered",
                             resource_type="material", resource_id=material_id,
                             detail={"code": code, "hazard_class": hazard_class},
                             occurred_at=self._now())
                return "material", material_id, {"material_id": material_id, "code": code}

            receipt = self._idempotent(conn, request_id=request_id, action="register_material",
                                       payload=payload, create=create)
            return {"material_id": material_id, **receipt.__dict__}

    def _material_by_code(self, conn, site_id: str, material_code: str):
        row = conn.execute(
            "SELECT * FROM materials WHERE site_id=? AND code=? AND active=1", (site_id, material_code)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"物料 {material_code} 不存在或已停用")
        return row

    # ------------------------------------------------------------ 储备与配额

    def add_critical_reserve(self, *, request_id: str, actor_id: str, voyage_id: str,
                             material_code: str, quantity: float, unit: str) -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
                               material_code=material_code, quantity=quantity, unit=unit)
        quantity = positive(quantity, "quantity")
        unit = self._text(unit, "unit", 40) if not isinstance(unit, str) else unit.strip()
        if not unit:
            raise ValidationError("unit 不能为空")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            replay = self._peek_replay(conn, request_id=request_id, action="add_critical_reserve",
                                       payload=payload)
            if replay is not None:
                return replay
            voyage = self._voyage(conn, voyage_id)
            self._require_open(voyage)
            self._material_by_code(conn, voyage["site_id"], material_code)

            def create():
                conn.execute(
                    "INSERT INTO critical_reserves(voyage_id,material_code,quantity,unit,created_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(voyage_id,material_code) DO UPDATE SET "
                    "quantity=excluded.quantity, unit=excluded.unit",
                    (voyage_id, material_code, quantity, unit, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="critical_reserve.set",
                             resource_type="voyage", resource_id=voyage_id,
                             detail={"material_code": material_code, "quantity": quantity, "unit": unit},
                             occurred_at=self._now())
                return "critical_reserve", f"{voyage_id}:{material_code}", {
                    "voyage_id": voyage_id, "material_code": material_code, "quantity": quantity}

            receipt = self._idempotent(conn, request_id=request_id, action="add_critical_reserve",
                                       payload=payload, create=create)
            return {"voyage_id": voyage_id, "material_code": material_code, **receipt.__dict__}

    def set_org_quota(self, *, request_id: str, actor_id: str, voyage_id: str,
                      organization_id: str, material_code: str,
                      max_quantity: float | None = None, max_weight: float | None = None) -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
                               organization_id=organization_id, material_code=material_code,
                               max_quantity=max_quantity, max_weight=max_weight)
        if max_quantity is not None:
            max_quantity = non_negative(max_quantity, "max_quantity")
        if max_weight is not None:
            max_weight = non_negative(max_weight, "max_weight")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            replay = self._peek_replay(conn, request_id=request_id, action="set_org_quota",
                                       payload=payload)
            if replay is not None:
                return replay
            voyage = self._voyage(conn, voyage_id)
            self._require_open(voyage)
            self._material_by_code(conn, voyage["site_id"], material_code)
            if conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                            (organization_id,)).fetchone() is None:
                raise NotFoundError("机构不存在")

            def create():
                conn.execute(
                    "INSERT INTO org_quotas(voyage_id,organization_id,material_code,max_quantity,max_weight,created_at) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(voyage_id,organization_id,material_code) DO UPDATE SET "
                    "max_quantity=excluded.max_quantity, max_weight=excluded.max_weight",
                    (voyage_id, organization_id, material_code, max_quantity, max_weight, self._now()),
                )
                append_event(conn, actor_id=actor_id, action="org_quota.set", resource_type="voyage",
                             resource_id=voyage_id,
                             detail={"organization_id": organization_id, "material_code": material_code,
                                     "max_quantity": max_quantity, "max_weight": max_weight},
                             occurred_at=self._now())
                return "org_quota", f"{voyage_id}:{organization_id}:{material_code}", {
                    "voyage_id": voyage_id, "organization_id": organization_id,
                    "material_code": material_code}

            receipt = self._idempotent(conn, request_id=request_id, action="set_org_quota",
                                       payload=payload, create=create)
            return {"voyage_id": voyage_id, "organization_id": organization_id,
                    "material_code": material_code, **receipt.__dict__}

    # ------------------------------------------------------------------ 申报

    def submit_application(self, *, request_id: str, actor_id: str, voyage_id: str, code: str,
                           organization_id: str, material_code: str, quantity: float,
                           batch_expiry_at: str | None = None, priority_score: int = 0) -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id, code=code,
                               organization_id=organization_id, material_code=material_code,
                               quantity=quantity, batch_expiry_at=batch_expiry_at,
                               priority_score=priority_score)
        quantity = positive(quantity, "quantity")
        priority_score = _priority(priority_score)
        expiry = parse_ts(batch_expiry_at, "batch_expiry_at") if batch_expiry_at else None
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            replay = self._peek_replay(conn, request_id=request_id, action="submit_application",
                                       payload=payload)
            if replay is not None:
                return replay
            voyage = self._voyage(conn, voyage_id)
            self._require_open(voyage)
            if actor.role != "admin" and actor.organization_id != organization_id:
                raise PermissionDenied("不能替其他机构提交申报")
            if conn.execute("SELECT 1 FROM organizations WHERE organization_id=?",
                            (organization_id,)).fetchone() is None:
                raise NotFoundError("机构不存在")
            material = self._material_by_code(conn, voyage["site_id"], material_code)
            code = self._identifier(code, "application code")
            self._check_org_quota(conn, voyage_id, organization_id, material_code, quantity, added=0.0)
            application_id = uuid.uuid4().hex
            now = self._now()

            def create():
                try:
                    conn.execute(
                        "INSERT INTO applications(application_id,voyage_id,code,organization_id,material_code,"
                        "quantity,unit_weight,unit_volume,hazard_class,batch_expiry_at,priority_score,"
                        "is_emergency,status,decision_reason_json,version,created_by,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,0,'submitted','[]',1,?,?,?)",
                        (application_id, voyage_id, code, organization_id, material_code, quantity,
                         material["unit_weight"], material["unit_volume"], material["hazard_class"],
                         expiry.isoformat() if expiry else None, priority_score, actor_id, now, now),
                    )
                except Exception as exc:
                    raise ConflictError("申报代码在该航次内已经存在") from exc
                conn.execute(
                    "INSERT INTO application_revisions(revision_id,application_id,version,payload_json,"
                    "change_reason,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, application_id, 1,
                     canonical_json({"code": code, "organization_id": organization_id,
                                     "material_code": material_code, "quantity": quantity,
                                     "batch_expiry_at": expiry.isoformat() if expiry else None,
                                     "priority_score": priority_score}),
                     "初次提交", actor_id, now),
                )
                append_event(conn, actor_id=actor_id, action="application.submitted",
                             resource_type="application", resource_id=application_id,
                             detail={"voyage_id": voyage_id, "code": code,
                                     "material_code": material_code, "quantity": quantity},
                             occurred_at=now)
                return "application", application_id, {"application_id": application_id, "code": code}

            receipt = self._idempotent(conn, request_id=request_id, action="submit_application",
                                       payload=payload, create=create)
            return {"application_id": application_id, **receipt.__dict__}

    def revise_application(self, *, request_id: str, actor_id: str, voyage_id: str, application_id: str,
                           quantity: float | None = None, batch_expiry_at: str | None = None,
                           priority_score: int | None = None, change_reason: str = "") -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
                               application_id=application_id, quantity=quantity,
                               batch_expiry_at=batch_expiry_at, priority_score=priority_score,
                               change_reason=change_reason)
        change_reason = self._text(change_reason or "修订申报", "change_reason", 300)
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            replay = self._peek_replay(conn, request_id=request_id, action="revise_application",
                                       payload=payload)
            if replay is not None:
                return replay
            app = self._application(conn, voyage_id, application_id)
            voyage = self._voyage(conn, voyage_id)
            self._require_open(voyage)
            if actor.role != "admin" and actor.organization_id != app["organization_id"]:
                raise PermissionDenied("不能修订其他机构的申报")
            new_qty = app["quantity"] if quantity is None else positive(quantity, "quantity")
            new_priority = app["priority_score"] if priority_score is None else _priority(priority_score)
            if batch_expiry_at is None:
                new_expiry = app["batch_expiry_at"]
            elif batch_expiry_at == "":
                new_expiry = None
            else:
                new_expiry = parse_ts(batch_expiry_at, "batch_expiry_at").isoformat()
            self._check_org_quota(conn, voyage_id, app["organization_id"], app["material_code"],
                                  new_qty, added=-app["quantity"])
            new_version = app["version"] + 1
            now = self._now()

            def create():
                conn.execute(
                    "UPDATE applications SET quantity=?, batch_expiry_at=?, priority_score=?, version=?, "
                    "updated_at=? WHERE application_id=?",
                    (new_qty, new_expiry, new_priority, new_version, now, application_id),
                )
                conn.execute(
                    "INSERT INTO application_revisions(revision_id,application_id,version,payload_json,"
                    "change_reason,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, application_id, new_version,
                     canonical_json({"quantity": new_qty, "batch_expiry_at": new_expiry,
                                     "priority_score": new_priority}),
                     change_reason, actor_id, now),
                )
                append_event(conn, actor_id=actor_id, action="application.revised",
                             resource_type="application", resource_id=application_id,
                             detail={"version": new_version, "change_reason": change_reason},
                             occurred_at=now)
                return "application", application_id, {"application_id": application_id,
                                                       "version": new_version}

            receipt = self._idempotent(conn, request_id=request_id, action="revise_application",
                                       payload=payload, create=create)
            return {"application_id": application_id, "version": new_version, **receipt.__dict__}

    def cancel_application(self, *, request_id: str, actor_id: str, voyage_id: str,
                           application_id: str, reason: str = "") -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
                               application_id=application_id, reason=reason)
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            replay = self._peek_replay(conn, request_id=request_id, action="cancel_application",
                                       payload=payload)
            if replay is not None:
                return replay
            app = self._application(conn, voyage_id, application_id)
            voyage = self._voyage(conn, voyage_id)
            self._require_open(voyage)
            if actor.role != "admin" and actor.organization_id != app["organization_id"]:
                raise PermissionDenied("不能撤销其他机构的申报")

            def create():
                conn.execute(
                    "UPDATE applications SET status='cancelled', updated_at=? WHERE application_id=?",
                    (self._now(), application_id),
                )
                append_event(conn, actor_id=actor_id, action="application.cancelled",
                             resource_type="application", resource_id=application_id,
                             detail={"reason": reason}, occurred_at=self._now())
                return "application", application_id, {"application_id": application_id,
                                                       "status": "cancelled"}

            receipt = self._idempotent(conn, request_id=request_id, action="cancel_application",
                                       payload=payload, create=create)
            return {"application_id": application_id, **receipt.__dict__}

    def list_applications(self, voyage_id: str, status: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM applications WHERE voyage_id=?"
        params: list[Any] = [voyage_id]
        if status:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY created_at, application_id"
        return [row_dict(row) for row in self.database.connection.execute(query, params)]

    def get_application(self, voyage_id: str, application_id: str) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM applications WHERE voyage_id=? AND application_id=?",
            (voyage_id, application_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("申报不存在")
        result = row_dict(row)
        result["decision_reason"] = json.loads(result.pop("decision_reason_json"))
        return result

    # ---------------------------------------------------------- 替代拆分顺延

    def record_relation(self, *, request_id: str, actor_id: str, voyage_id: str,
                        from_application_id: str, kind: str, to_application_id: str | None = None,
                        quantity: float | None = None, successor_voyage_id: str | None = None,
                        note: str = "") -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
                               from_application_id=from_application_id, kind=kind,
                               to_application_id=to_application_id, quantity=quantity,
                               successor_voyage_id=successor_voyage_id, note=note)
        if kind not in {"substitute", "split", "defer"}:
            raise ValidationError("kind 必须是 substitute、split 或 defer")
        note = note.strip() if isinstance(note, str) else ""
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            replay = self._peek_replay(conn, request_id=request_id, action="record_relation",
                                       payload=payload)
            if replay is not None:
                return replay
            voyage = self._voyage(conn, voyage_id)
            self._require_open(voyage)
            source = self._application(conn, voyage_id, from_application_id)
            if actor.role != "admin" and actor.organization_id != source["organization_id"]:
                raise PermissionDenied("不能为其他机构的申报登记关系")
            target = None
            if to_application_id:
                target = self._application(conn, voyage_id, to_application_id)
                if target["application_id"] == source["application_id"]:
                    raise ValidationError("关系不能指向申报自身")
            if kind in {"substitute", "split"} and target is None:
                raise ValidationError(f"{kind} 关系必须指定 to_application_id")
            qty_value: float | None = None
            if quantity is not None:
                qty_value = positive(quantity, "quantity")
                if kind == "split" and qty_value > source["quantity"] + EPS:
                    raise ValidationError("拆分数量不能超过源申报数量")
            successor = None
            if kind == "defer":
                successor = successor_voyage_id or voyage_id
                if successor != voyage_id:
                    succ_row = conn.execute("SELECT 1 FROM voyages WHERE voyage_id=?", (successor,)).fetchone()
                    if succ_row is None:
                        raise NotFoundError("顺延目标航次不存在")
            else:
                successor = None

            def create():
                existing = conn.execute(
                    "SELECT * FROM application_relations WHERE voyage_id=? AND from_application_id=? "
                    "AND kind=? AND COALESCE(to_application_id,'')=COALESCE(?, '') "
                    "AND COALESCE(successor_voyage_id,'')=COALESCE(?, '') AND active=1",
                    (voyage_id, from_application_id, kind, to_application_id, successor),
                ).fetchone()
                version = 1
                supersedes = None
                if existing:
                    version = existing["version"] + 1
                    supersedes = existing["relation_id"]
                    conn.execute("UPDATE application_relations SET active=0 WHERE relation_id=?",
                                 (existing["relation_id"],))
                relation_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO application_relations(relation_id,voyage_id,from_application_id,"
                    "to_application_id,kind,quantity,successor_voyage_id,note,version,active,"
                    "supersedes_relation_id,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)",
                    (relation_id, voyage_id, from_application_id, to_application_id, kind, qty_value,
                     successor, note, version, supersedes, actor_id, self._now()),
                )
                append_event(conn, actor_id=actor_id, action=f"relation.{kind}.recorded",
                             resource_type="relation", resource_id=relation_id,
                             detail={"voyage_id": voyage_id, "from": from_application_id,
                                     "to": to_application_id, "version": version,
                                     "supersedes": supersedes, "quantity": qty_value,
                                     "successor_voyage_id": successor, "note": note},
                             occurred_at=self._now())
                return "relation", relation_id, {"relation_id": relation_id, "version": version}

            receipt = self._idempotent(conn, request_id=request_id, action="record_relation",
                                       payload=payload, create=create)
            relation_row = conn.execute(
                "SELECT version FROM application_relations WHERE relation_id=?",
                (receipt.resource_id,)).fetchone()
            return {"relation_id": receipt.resource_id,
                    "version": relation_row["version"] if relation_row else 1,
                    "request_id": receipt.request_id, "replayed": receipt.replayed}

    def list_relations(self, voyage_id: str, *, include_superseded: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM application_relations WHERE voyage_id=?"
        if not include_superseded:
            query += " AND active=1"
        query += " ORDER BY rowid"
        return [row_dict(row) for row in self.database.connection.execute(query, (voyage_id,))]

    # ------------------------------------------------------------------ 冻结

    def freeze_voyage(self, *, request_id: str, actor_id: str, voyage_id: str) -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id)
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            replay = self._peek_replay(conn, request_id=request_id, action="freeze_voyage",
                                       payload=payload)
            if replay is not None:
                voyage_row = self._voyage(conn, voyage_id)
                snap = conn.execute(
                    "SELECT manifest_hash FROM freeze_snapshots WHERE voyage_id=? AND freeze_version=?",
                    (voyage_id, voyage_row["freeze_version"])).fetchone()
                return {"voyage_id": voyage_id, "freeze_version": voyage_row["freeze_version"],
                        "manifest_hash": snap["manifest_hash"], "replayed": True}
            voyage = self._voyage(conn, voyage_id)
            if voyage["status"] != "open":
                raise ConflictError("航次已经冻结，不能重复固化")
            now = self.clock.now()
            deadline = datetime.fromisoformat(voyage["deadline_at"])
            if now < deadline:
                raise ConflictError("未到申报截止时刻，不能提前固化")
            plan = self._compute_freeze(conn, voyage)

            def create():
                ts = self._now()
                for item in plan["items"]:
                    conn.execute(
                        "INSERT INTO allocations(allocation_id,voyage_id,freeze_version,application_id,"
                        "compartment,allocated_qty,loaded_qty,issued_qty,weight_qty,volume_qty,rank,"
                        "state,reason_json,updated_at) VALUES(?,?,1,?,?,?,0,0,?,?,?,?,?,?)",
                        (uuid.uuid4().hex, voyage_id, item["application_id"], item["compartment"],
                         item["allocated_qty"], item["weight_qty"], item["volume_qty"],
                         item["rank"], item["state"], canonical_json(item["reasons"]), ts),
                    )
                    self._ledger(conn, voyage_id=voyage_id, application_id=item["application_id"],
                                 compartment=item["compartment"],
                                 event_type=item["initial_event"], delta_qty=item["allocated_qty"],
                                 balance_after=item["allocated_qty"], reason=item["reasons"],
                                 created_by=actor_id, created_at=ts)
                    conn.execute(
                        "UPDATE applications SET status=?, decision_reason_json=?, decided_at=?, updated_at=? "
                        "WHERE application_id=?",
                        (item["application_status"], canonical_json(item["reasons"]), ts, ts,
                         item["application_id"]),
                    )
                manifest = self._build_manifest(conn, voyage, plan)
                manifest_hash = digest(manifest)
                conn.execute(
                    "INSERT INTO freeze_snapshots(voyage_id,freeze_version,manifest_json,manifest_hash,"
                    "created_by,created_at) VALUES(?,1,?,?,?,?)",
                    (voyage_id, canonical_json(manifest), manifest_hash, actor_id, ts),
                )
                conn.execute(
                    "UPDATE voyages SET status='frozen', frozen_at=?, freeze_version=1 "
                    "WHERE voyage_id=?",
                    (ts, voyage_id),
                )
                append_event(conn, actor_id=actor_id, action="voyage.frozen", resource_type="voyage",
                             resource_id=voyage_id,
                             detail={"freeze_version": 1, "manifest_hash": manifest_hash,
                                     "summary": plan["summary"]},
                             occurred_at=ts)
                return "freeze", voyage_id, {"voyage_id": voyage_id, "freeze_version": 1,
                                             "manifest_hash": manifest_hash, "summary": plan["summary"]}

            receipt = self._idempotent(conn, request_id=request_id, action="freeze_voyage",
                                       payload=payload, create=create)
            snap = conn.execute(
                "SELECT manifest_hash FROM freeze_snapshots WHERE voyage_id=?", (voyage_id,)
            ).fetchone()
            return {"voyage_id": voyage_id, "freeze_version": 1,
                    "manifest_hash": snap["manifest_hash"], "replayed": receipt.replayed}

    def _compute_freeze(self, conn, voyage) -> dict[str, Any]:
        voyage_id = voyage["voyage_id"]
        reserves = {row["material_code"]: row["quantity"] for row in conn.execute(
            "SELECT material_code, quantity FROM critical_reserves WHERE voyage_id=?", (voyage_id,))}
        quotas = {}
        for row in conn.execute("SELECT * FROM org_quotas WHERE voyage_id=?", (voyage_id,)):
            quotas[(row["organization_id"], row["material_code"])] = (row["max_quantity"], row["max_weight"])
        apps = [row_dict(r) for r in conn.execute(
            "SELECT * FROM applications WHERE voyage_id=? AND status!='cancelled'", (voyage_id,))]
        winter_end = datetime.fromisoformat(voyage["winter_end_at"])
        now = self.clock.now()

        valid: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        for app in apps:
            if app["batch_expiry_at"]:
                expiry = datetime.fromisoformat(app["batch_expiry_at"])
                if expiry <= now:
                    items.append(self._plan_item(app, 0.0, rank=10**9, state="rejected",
                                                 reasons=[{"code": "batch_already_expired",
                                                           "detail": "批次在截止时已过效期"}]))
                    continue
                if expiry < winter_end:
                    items.append(self._plan_item(app, 0.0, rank=10**9, state="rejected",
                                                 reasons=[{"code": "batch_expires_before_winter_end",
                                                           "detail": "批次效期早于越冬结束，无法覆盖越冬需求"}]))
                    continue
            valid.append(app)
        order = sorted(valid, key=lambda a: (-a["priority_score"], a["created_at"], a["application_id"]))

        limits = {
            "dry": (voyage["dry_weight_capacity"] - voyage["reserved_dry_weight"],
                    voyage["dry_volume_capacity"] - voyage["reserved_dry_volume"]),
            "hazmat": (voyage["hazmat_weight_capacity"] - voyage["reserved_hazmat_weight"],
                       voyage["hazmat_volume_capacity"] - voyage["reserved_hazmat_volume"]),
        }
        used = {"dry": [0.0, 0.0], "hazmat": [0.0, 0.0]}
        planned: dict[str, float] = {}
        quota_used: dict[tuple[str, str], float] = {}
        covered: dict[str, float] = {code: 0.0 for code in reserves}

        def room_for(app):
            comp = compartment_of(app["hazard_class"])
            w_left = limits[comp][0] - used[comp][0]
            v_left = limits[comp][1] - used[comp][1]
            q_by_w = w_left / app["unit_weight"] if app["unit_weight"] else float("inf")
            q_by_v = v_left / app["unit_volume"] if app["unit_volume"] else float("inf")
            qq = quotas.get((app["organization_id"], app["material_code"]))
            q_left = float("inf")
            if qq and qq[0] is not None:
                q_left = min(q_left, qq[0] - quota_used.get((app["organization_id"], app["material_code"]), 0.0))
            if qq and qq[1] is not None:
                weight_used = quota_used.get((app["organization_id"], app["material_code"]), 0.0) * app["unit_weight"]
                q_left = min(q_left, (qq[1] - weight_used) / app["unit_weight"])
            return max(0.0, min(app["quantity"] - planned.get(app["application_id"], 0.0),
                                q_by_w, q_by_v, q_left))

        # 第一轮：优先满足关键储备
        for app in order:
            need = reserves.get(app["material_code"])
            if need is None:
                continue
            shortfall = need - covered[app["material_code"]]
            if shortfall <= EPS:
                continue
            q = min(shortfall, room_for(app))
            if q > EPS:
                self._absorb(app, q, used, planned, quota_used)
                covered[app["material_code"]] += q

        infeasible = []
        for code, need in reserves.items():
            gap = need - covered[code]
            if gap > 1e-6:
                supply = sum(a["quantity"] for a in valid if a["material_code"] == code)
                reason_code = "reserve_supply_insufficient" if supply + EPS < need \
                    else "reserve_capacity_or_quota_insufficient"
                infeasible.append({"material_code": code, "required": need,
                                   "covered": round(covered[code], 6), "shortfall": round(gap, 6),
                                   "reason": reason_code})
        if infeasible:
            raise InfeasibleError(json.dumps({"infeasible_reserves": infeasible}, ensure_ascii=False))

        # 第二轮：剩余容量按科研优先级竞争
        for rank, app in enumerate(order, start=1):
            q = room_for(app)
            if q > EPS:
                self._absorb(app, q, used, planned, quota_used)
            allocated = planned.get(app["application_id"], 0.0)
            if allocated <= EPS:
                reasons = self._capacity_reasons(app, used, limits, quotas, quota_used, planned)
                items.append(self._plan_item(app, 0.0, rank=rank, state="waitlisted", reasons=reasons))
            elif allocated + EPS < app["quantity"]:
                items.append(self._plan_item(
                    app, allocated, rank=rank, state="partial",
                    reasons=[{"code": "partial_capacity",
                              "detail": "受舱位重量或体积限制仅获部分配额，余量进入候补"}]))
            else:
                reasons = [{"code": "rank_allocated",
                            "detail": f"科研优先级 {app['priority_score']}，排序位次 {rank}，舱位充足"}]
                if app["material_code"] in reserves:
                    reasons.append({"code": "critical_reserve_contributor",
                                    "detail": "该申报承担关键储备覆盖"})
                items.append(self._plan_item(app, allocated, rank=rank, state="allocated", reasons=reasons))

        winners_by_material: dict[str, list[str]] = {}
        for item in items:
            if item["state"] in {"allocated", "partial"}:
                winners_by_material.setdefault(item["material_code"], []).append(item["code"])
        for item in items:
            if item["state"] == "waitlisted" and item["material_code"] in winners_by_material:
                item["reasons"].append({
                    "code": "duplicate_material",
                    "detail": "同物料已有更高优先级申报获批："
                              + "、".join(winners_by_material[item["material_code"]]),
                })

        summary = {
            "applications": len(apps),
            "approved": sum(1 for i in items if i["state"] in {"allocated", "partial"}),
            "fully_allocated": sum(1 for i in items if i["state"] == "allocated"),
            "partial": sum(1 for i in items if i["state"] == "partial"),
            "waitlisted": sum(1 for i in items if i["state"] == "waitlisted"),
            "rejected": sum(1 for i in items if i["state"] == "rejected"),
            "dry_weight_used": round(used["dry"][0], 6),
            "dry_volume_used": round(used["dry"][1], 6),
            "hazmat_weight_used": round(used["hazmat"][0], 6),
            "hazmat_volume_used": round(used["hazmat"][1], 6),
            "critical_reserves_met": len(reserves),
        }
        return {"items": items, "summary": summary}

    @staticmethod
    def _absorb(app, q, used, planned, quota_used):
        comp = compartment_of(app["hazard_class"])
        used[comp][0] += q * app["unit_weight"]
        used[comp][1] += q * app["unit_volume"]
        planned[app["application_id"]] = planned.get(app["application_id"], 0.0) + q
        key = (app["organization_id"], app["material_code"])
        quota_used[key] = quota_used.get(key, 0.0) + q

    @staticmethod
    def _plan_item(app, allocated, *, rank, state, reasons):
        return {
            "application_id": app["application_id"],
            "code": app["code"],
            "organization_id": app["organization_id"],
            "material_code": app["material_code"],
            "hazard_class": app["hazard_class"],
            "compartment": compartment_of(app["hazard_class"]),
            "requested_qty": app["quantity"],
            "allocated_qty": round(allocated, 9),
            "weight_qty": round(allocated * app["unit_weight"], 9),
            "volume_qty": round(allocated * app["unit_volume"], 9),
            "rank": rank,
            "state": state,
            "reasons": reasons,
            "initial_event": {"allocated": "allocated", "partial": "partial",
                              "waitlisted": "waitlisted", "rejected": "rejected"}[state],
            "application_status": {"allocated": "approved", "partial": "approved",
                                   "waitlisted": "waitlisted", "rejected": "rejected"}[state],
        }

    @staticmethod
    def _capacity_reasons(app, used, limits, quotas, quota_used, planned):
        comp = compartment_of(app["hazard_class"])
        reasons = []
        remaining = app["quantity"] - planned.get(app["application_id"], 0.0)
        w = remaining * app["unit_weight"]
        v = remaining * app["unit_volume"]
        w_left = limits[comp][0] - used[comp][0]
        v_left = limits[comp][1] - used[comp][1]
        if w > w_left + EPS:
            reasons.append({"code": "capacity_weight",
                            "detail": f"{comp} 舱剩余承重 {round(w_left, 3)} 小于需求 {round(w, 3)}"})
        if v > v_left + EPS:
            reasons.append({"code": "capacity_volume",
                            "detail": f"{comp} 舱剩余容积 {round(v_left, 3)} 小于需求 {round(v, 3)}"})
        qq = quotas.get((app["organization_id"], app["material_code"]))
        if qq and qq[0] is not None and quota_used.get((app["organization_id"], app["material_code"]), 0.0) + EPS >= qq[0]:
            reasons.append({"code": "org_quota_quantity",
                            "detail": f"机构 {app['organization_id']} 对该物料的申报总量已达配额 {qq[0]}"})
        if not reasons:
            reasons.append({"code": "lower_priority", "detail": "排序靠后，轮到时舱位已被占满"})
        return reasons

    def _build_manifest(self, conn, voyage, plan) -> dict[str, Any]:
        apps = [row_dict(r) for r in conn.execute(
            "SELECT * FROM applications WHERE voyage_id=? ORDER BY created_at, application_id", (voyage["voyage_id"],))]
        for app in apps:
            app.pop("decision_reason_json", None)
        revisions = [row_dict(r) for r in conn.execute(
            "SELECT * FROM application_revisions r WHERE r.application_id IN "
            "(SELECT application_id FROM applications WHERE voyage_id=?) ORDER BY r.created_at",
            (voyage["voyage_id"],))]
        relations = [row_dict(r) for r in conn.execute(
            "SELECT * FROM application_relations WHERE voyage_id=? ORDER BY created_at, relation_id",
            (voyage["voyage_id"],))]
        reserves = [row_dict(r) for r in conn.execute(
            "SELECT * FROM critical_reserves WHERE voyage_id=?", (voyage["voyage_id"],))]
        qs = [row_dict(r) for r in conn.execute(
            "SELECT * FROM org_quotas WHERE voyage_id=?", (voyage["voyage_id"],))]
        voyage_view = {k: voyage[k] for k in voyage.keys()}
        return {"voyage": voyage_view, "critical_reserves": reserves, "org_quotas": qs,
                "applications": apps, "application_revisions": revisions,
                "relations": relations, "decision": plan}

    def freeze_manifest(self, voyage_id: str, freeze_version: int = 1) -> dict[str, Any]:
        row = self.database.connection.execute(
            "SELECT * FROM freeze_snapshots WHERE voyage_id=? AND freeze_version=?",
            (voyage_id, freeze_version),
        ).fetchone()
        if row is None:
            raise NotFoundError("冻结清单不存在")
        return {"manifest_hash": row["manifest_hash"], "manifest": json.loads(row["manifest_json"]),
                "created_by": row["created_by"], "created_at": row["created_at"]}

    # ------------------------------------------------------------ 紧急预留

    def request_emergency_release(self, *, request_id: str, actor_id: str, voyage_id: str,
                                  organization_id: str, material_code: str, quantity: float,
                                  reason: str, valid_minutes: int = 60) -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
                               organization_id=organization_id, material_code=material_code,
                               quantity=quantity, reason=reason, valid_minutes=valid_minutes)
        quantity = positive(quantity, "quantity")
        reason = self._text(reason, "reason", 500)
        if not isinstance(valid_minutes, int) or not 1 <= valid_minutes <= 1440:
            raise ValidationError("valid_minutes 必须在 1 到 1440 之间")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            replay = self._peek_replay(conn, request_id=request_id,
                                       action="request_emergency_release", payload=payload)
            if replay is not None:
                return replay
            voyage = self._voyage(conn, voyage_id)
            if voyage["status"] != "frozen":
                raise ConflictError("只有在截止冻结后才能申请紧急预留")
            if actor.role != "admin" and actor.organization_id != organization_id:
                raise PermissionDenied("不能替其他机构申请紧急需求")
            material = self._material_by_code(conn, voyage["site_id"], material_code)
            now_dt = self.clock.now()
            expires = now_dt + timedelta(minutes=valid_minutes)
            release_id = uuid.uuid4().hex
            application_id = uuid.uuid4().hex

            def create():
                ts = self._now()
                conn.execute(
                    "INSERT INTO applications(application_id,voyage_id,code,organization_id,material_code,"
                    "quantity,unit_weight,unit_volume,hazard_class,batch_expiry_at,priority_score,"
                    "is_emergency,status,decision_reason_json,version,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,NULL,100,1,'submitted','[]',1,?,?,?)",
                    (application_id, voyage_id, f"EMG-{release_id[:12]}", organization_id, material_code,
                     quantity, material["unit_weight"], material["unit_volume"], material["hazard_class"],
                     actor_id, ts, ts),
                )
                conn.execute(
                    "INSERT INTO emergency_releases(release_id,voyage_id,application_id,quantity,reason,"
                    "status,expires_at,created_by,created_at) VALUES(?,?,?,?,?, 'pending', ?,?,?)",
                    (release_id, voyage_id, application_id, quantity, reason,
                     expires.isoformat(), actor_id, ts),
                )
                append_event(conn, actor_id=actor_id, action="emergency.requested",
                             resource_type="emergency_release", resource_id=release_id,
                             detail={"voyage_id": voyage_id, "application_id": application_id,
                                     "material_code": material_code, "quantity": quantity,
                                     "expires_at": expires.isoformat(), "reason": reason},
                             occurred_at=ts)
                return "emergency_release", release_id, {"release_id": release_id,
                                                         "application_id": application_id}

            receipt = self._idempotent(conn, request_id=request_id, action="request_emergency_release",
                                       payload=payload, create=create)
            return {"release_id": receipt.resource_id, "application_id": application_id,
                    "replayed": receipt.replayed, "expires_at": expires.isoformat()}

    def approve_emergency_release(self, *, request_id: str, actor_id: str, release_id: str) -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, release_id=release_id)
        # 先在独立短事务里惰性过期窗口，保证过期状态不会因随后的业务回滚而丢失
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *AUTHORIZED_EMERGENCY_ROLES)
            row = conn.execute("SELECT * FROM emergency_releases WHERE release_id=?",
                               (release_id,)).fetchone()
            if row is None:
                raise NotFoundError("紧急申请不存在")
            if row["status"] == "pending" and \
               datetime.fromisoformat(row["expires_at"]) <= self.clock.now():
                conn.execute("UPDATE emergency_releases SET status='expired', finalized_at=? WHERE release_id=?",
                             (self._now(), release_id))
                append_event(conn, actor_id=actor_id, action="emergency.expired",
                             resource_type="emergency_release", resource_id=release_id, detail={},
                             occurred_at=self._now())
                expired = True
            else:
                expired = False
        if expired:
            raise ConflictError("紧急会签窗口已过期，预留量未被动用")
        with self.database.transaction(immediate=True) as conn:
            self._actor(conn, actor_id)
            replay = self._peek_replay(conn, request_id=request_id,
                                       action="approve_emergency_release", payload=payload)
            if replay is not None:
                return replay
            row = conn.execute("SELECT * FROM emergency_releases WHERE release_id=?", (release_id,)).fetchone()
            if row["status"] not in {"pending", "approved"}:
                raise ConflictError(f"紧急申请状态为 {row['status']}，不能再批准")
            first, second = row["approval1_by"], row["approval2_by"]
            if actor_id in {first, second}:
                raise ConflictError("同一名授权人不能重复会签")
            ts = self._now()
            finalized = False
            if first is None:
                conn.execute("UPDATE emergency_releases SET approval1_by=?, approval1_at=? WHERE release_id=?",
                             (actor_id, ts, release_id))
                append_event(conn, actor_id=actor_id, action="emergency.approval1",
                             resource_type="emergency_release", resource_id=release_id, detail={},
                             occurred_at=ts)
                status = "awaiting_second_approval"
            else:
                # 第二次会签：先校验预留池余量，不足则整笔回滚，首签保留以便余量恢复后重试
                self._assert_reserve_capacity(conn, row)
                conn.execute("UPDATE emergency_releases SET approval2_by=?, approval2_at=?, status='approved',"
                             "finalized_at=? WHERE release_id=?", (actor_id, ts, ts, release_id))
                finalized = True
                status = "approved"

            result = {"release_id": release_id, "status": status}
            if finalized:
                self._consume_emergency(conn, row, actor_id, ts)

            def create():
                return "emergency_approval", release_id, result

            receipt = self._idempotent(conn, request_id=request_id, action="approve_emergency_release",
                                       payload=payload, create=create)
            return {**result, "replayed": receipt.replayed}

    def _assert_reserve_capacity(self, conn, release) -> None:
        """校验紧急申请的量在对应舱预留池余额内，不足则拒绝（由调用方回滚）。"""

        voyage = self._voyage(conn, release["voyage_id"])
        app = conn.execute("SELECT * FROM applications WHERE application_id=?",
                           (release["application_id"],)).fetchone()
        qty = release["quantity"]
        comp = compartment_of(app["hazard_class"])
        if voyage[f"reserved_{comp}_weight_used"] + qty * app["unit_weight"] \
                > voyage[f"reserved_{comp}_weight"] + EPS or \
           voyage[f"reserved_{comp}_volume_used"] + qty * app["unit_volume"] \
                > voyage[f"reserved_{comp}_volume"] + EPS:
            raise ConflictError("预留量余额不足，无法满足该紧急申请")

    def _consume_emergency(self, conn, release, actor_id: str, ts: str) -> None:
        voyage = self._voyage(conn, release["voyage_id"])
        app = conn.execute("SELECT * FROM applications WHERE application_id=?",
                           (release["application_id"],)).fetchone()
        qty = release["quantity"]
        weight = qty * app["unit_weight"]
        volume = qty * app["unit_volume"]
        comp = compartment_of(app["hazard_class"])
        conn.execute(
            f"UPDATE voyages SET reserved_{comp}_weight_used=reserved_{comp}_weight_used+?, "
            f"reserved_{comp}_volume_used=reserved_{comp}_volume_used+? WHERE voyage_id=?",
            (weight, volume, voyage["voyage_id"]),
        )
        rank = 1_000_000
        conn.execute(
            "INSERT INTO allocations(allocation_id,voyage_id,freeze_version,application_id,compartment,"
            "allocated_qty,loaded_qty,issued_qty,weight_qty,volume_qty,rank,state,reason_json,updated_at) "
            "VALUES(?,?,1,?,?,?,0,0,?,?,?, 'allocated', ?,?)",
            (uuid.uuid4().hex, voyage["voyage_id"], app["application_id"], comp, qty, weight, volume,
             rank, canonical_json([{"code": "emergency_reserve",
                                    "detail": "截止后双授权会签，动用预留量"}]), ts),
        )
        self._ledger(conn, voyage_id=voyage["voyage_id"], application_id=app["application_id"],
                     compartment=comp, event_type="emergency_allocated", delta_qty=qty,
                     balance_after=qty, reason=[{"code": "emergency_reserve",
                                                 "release_id": release["release_id"]}],
                     created_by=actor_id, created_at=ts)
        conn.execute(
            "UPDATE applications SET status='approved', decision_reason_json=?, decided_at=?, updated_at=? "
            "WHERE application_id=?",
            (canonical_json([{"code": "emergency_reserve", "detail": "双授权会签批准动用预留量"}]),
             ts, ts, app["application_id"]),
        )
        append_event(conn, actor_id=actor_id, action="emergency.consumed",
                     resource_type="emergency_release", resource_id=release["release_id"],
                     detail={"application_id": app["application_id"], "quantity": qty,
                             "weight": weight, "volume": volume}, occurred_at=ts)

    def _release_emergency_back(self, conn, voyage, alloc, app, qty, event_type,
                                reason_code, reason_text, actor_id, ts) -> None:
        """把紧急份额退回预留池（短少/失效），不进入普通递补。"""

        comp = alloc["compartment"]
        weight = qty * app["unit_weight"]
        volume = qty * app["unit_volume"]
        conn.execute(
            f"UPDATE voyages SET reserved_{comp}_weight_used=MAX(0, reserved_{comp}_weight_used-?), "
            f"reserved_{comp}_volume_used=MAX(0, reserved_{comp}_volume_used-?) WHERE voyage_id=?",
            (weight, volume, voyage["voyage_id"]),
        )
        new_qty = round(alloc["allocated_qty"] - qty, 9)
        new_state = "partial" if new_qty > EPS else "released"
        conn.execute(
            "UPDATE allocations SET allocated_qty=?, weight_qty=?, volume_qty=?, state=?, updated_at=? "
            "WHERE allocation_id=?",
            (new_qty, round(new_qty * app["unit_weight"], 9),
             round(new_qty * app["unit_volume"], 9), new_state, ts, alloc["allocation_id"]),
        )
        self._ledger(conn, voyage_id=voyage["voyage_id"], application_id=app["application_id"],
                     compartment=comp, event_type="emergency_released", delta_qty=-qty,
                     balance_after=new_qty,
                     reason=[{"code": reason_code, "detail": reason_text,
                              "returned_to_reserve_pool": True}],
                     created_by=actor_id, created_at=ts)
        if new_qty <= EPS:
            conn.execute(
                "UPDATE applications SET status='waitlisted', updated_at=? WHERE application_id=?",
                (ts, app["application_id"]))

    def list_emergency_releases(self, voyage_id: str) -> list[dict[str, Any]]:
        return [row_dict(r) for r in self.database.connection.execute(
            "SELECT * FROM emergency_releases WHERE voyage_id=? ORDER BY created_at", (voyage_id,))]

    # ------------------------------------------------------------ 装载与领用

    def confirm_loading(self, *, request_id: str, actor_id: str, voyage_id: str,
                        application_id: str, quantity: float) -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
                               application_id=application_id, quantity=quantity)
        quantity = positive(quantity, "quantity")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES, "reviewer")
            replay = self._peek_replay(conn, request_id=request_id, action="confirm_loading",
                                       payload=payload)
            if replay is not None:
                return replay
            voyage = self._voyage(conn, voyage_id)
            if voyage["status"] != "frozen":
                raise ConflictError("航次未冻结，不能确认装载")
            alloc = self._allocation(conn, voyage_id, application_id)
            available = alloc["allocated_qty"] - alloc["loaded_qty"]
            if quantity > available + EPS:
                raise ConflictError(f"待装份额只有 {round(available, 6)}，不能确认装载 {quantity}")
            ts = self._now()

            def create():
                conn.execute(
                    "UPDATE allocations SET loaded_qty=loaded_qty+?, updated_at=? WHERE allocation_id=?",
                    (quantity, ts, alloc["allocation_id"]),
                )
                conn.execute(
                    "INSERT INTO loading_confirmations(confirmation_id,voyage_id,application_id,quantity,"
                    "request_id,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (uuid.uuid4().hex, voyage_id, application_id, quantity, request_id, actor_id, ts),
                )
                self._ledger(conn, voyage_id=voyage_id, application_id=application_id,
                             compartment=alloc["compartment"], event_type="loaded",
                             delta_qty=quantity, balance_after=alloc["allocated_qty"],
                             reason=[{"code": "loading_confirmed"}], created_by=actor_id, created_at=ts)
                append_event(conn, actor_id=actor_id, action="loading.confirmed",
                             resource_type="application", resource_id=application_id,
                             detail={"quantity": quantity}, occurred_at=ts)
                return "loading_confirmation", f"{voyage_id}:{application_id}", {
                    "voyage_id": voyage_id, "application_id": application_id,
                    "loaded_qty": alloc["loaded_qty"] + quantity}

            receipt = self._idempotent(conn, request_id=request_id, action="confirm_loading",
                                       payload=payload, create=create)
            return {"voyage_id": voyage_id, "application_id": application_id,
                    "loaded_qty": alloc["loaded_qty"] + quantity, "replayed": receipt.replayed}

    def confirm_issue(self, *, request_id: str, actor_id: str, voyage_id: str,
                      application_id: str, quantity: float) -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
                               application_id=application_id, quantity=quantity)
        quantity = positive(quantity, "quantity")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES, "reviewer")
            replay = self._peek_replay(conn, request_id=request_id, action="confirm_issue",
                                       payload=payload)
            if replay is not None:
                return replay
            alloc = self._allocation(conn, voyage_id, application_id)
            issuable = alloc["loaded_qty"] - alloc["issued_qty"]
            if quantity > issuable + EPS:
                raise ConflictError(f"已装未领份额只有 {round(issuable, 6)}")
            ts = self._now()

            def create():
                conn.execute(
                    "UPDATE allocations SET issued_qty=issued_qty+?, updated_at=? WHERE allocation_id=?",
                    (quantity, ts, alloc["allocation_id"]),
                )
                self._ledger(conn, voyage_id=voyage_id, application_id=application_id,
                             compartment=alloc["compartment"], event_type="issued",
                             delta_qty=quantity, balance_after=alloc["allocated_qty"],
                             reason=[{"code": "issued"}], created_by=actor_id, created_at=ts)
                append_event(conn, actor_id=actor_id, action="loading.issued",
                             resource_type="application", resource_id=application_id,
                             detail={"quantity": quantity}, occurred_at=ts)
                return "issue", f"{voyage_id}:{application_id}", {
                    "voyage_id": voyage_id, "application_id": application_id,
                    "issued_qty": alloc["issued_qty"] + quantity}

            receipt = self._idempotent(conn, request_id=request_id, action="confirm_issue",
                                       payload=payload, create=create)
            return {"voyage_id": voyage_id, "application_id": application_id,
                    "issued_qty": alloc["issued_qty"] + quantity, "replayed": receipt.replayed}

    def pending_loading(self, voyage_id: str) -> list[dict[str, Any]]:
        """返回重启后仍需接续的未完成装载确认。"""

        rows = self.database.connection.execute(
            "SELECT a.application_id, ap.code, ap.material_code, ap.organization_id, a.compartment, "
            "a.allocated_qty, a.loaded_qty, a.issued_qty, a.state, "
            "(a.allocated_qty-a.loaded_qty) AS outstanding_qty "
            "FROM allocations a JOIN applications ap ON ap.application_id=a.application_id "
            "WHERE a.voyage_id=? AND a.allocated_qty-a.loaded_qty>1e-9 "
            "ORDER BY a.rank, a.application_id", (voyage_id,))
        return [row_dict(r) for r in rows]

    def ledger(self, voyage_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM allocation_ledger WHERE voyage_id=? ORDER BY created_at, ledger_id", (voyage_id,))
        result = []
        for row in rows:
            item = row_dict(row)
            item["reason"] = json.loads(item.pop("reason_json"))
            result.append(item)
        return result

    # ------------------------------------------- 航班取消/短少/失效/临时换装

    def report_shortfall(self, *, request_id: str, actor_id: str, voyage_id: str,
                         application_id: str, missing_qty: float, reason: str = "") -> dict[str, Any]:
        return self._release_and_cascade(
            request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
            application_id=application_id, qty=positive(missing_qty, "missing_qty"),
            event_type="transferred_out", action="cargo.shortfall",
            reason_code="partial_arrival", reason_text=reason or "部分到货，未到份额释放转配",
            allow_unloaded_only=True)

    def report_expiry(self, *, request_id: str, actor_id: str, voyage_id: str,
                      application_id: str, quantity: float, reason: str = "") -> dict[str, Any]:
        return self._release_and_cascade(
            request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
            application_id=application_id, qty=positive(quantity, "quantity"),
            event_type="expired", action="cargo.expired",
            reason_code="material_expired", reason_text=reason or "物资失效，份额释放转配",
            allow_unloaded_only=True)

    def _release_and_cascade(self, *, request_id, actor_id, voyage_id, application_id, qty,
                             event_type, action, reason_code, reason_text, allow_unloaded_only):
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
                               application_id=application_id, qty=qty, event_type=event_type,
                               reason_code=reason_code, reason_text=reason_text)
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            replay = self._peek_replay(conn, request_id=request_id, action=action, payload=payload)
            if replay is not None:
                return replay
            voyage = self._voyage(conn, voyage_id)
            if voyage["status"] != "frozen":
                raise ConflictError("航次未冻结")
            alloc = self._allocation(conn, voyage_id, application_id)
            app = self._application(conn, voyage_id, application_id)
            releasable = alloc["allocated_qty"] - (alloc["loaded_qty"] if allow_unloaded_only else 0)
            if qty > releasable + EPS:
                raise ConflictError(f"可释放的未装载未领用份额只有 {round(releasable, 6)}")
            ts = self._now()
            releases: list[dict[str, Any]] = []
            created: dict[str, Any] = {}

            def create():
                is_emergency_app = conn.execute(
                    "SELECT 1 FROM applications WHERE application_id=? AND is_emergency=1",
                    (application_id,)).fetchone() is not None
                if is_emergency_app:
                    # 紧急份额来自独立预留池：释放时退回预留池，不能流入普通递补
                    self._release_emergency_back(conn, voyage, alloc, app, qty,
                                                 event_type, reason_code, reason_text, actor_id, ts)
                    response = {"voyage_id": voyage_id, "application_id": application_id,
                                "released_qty": qty, "reassigned": [],
                                "returned_to_reserve_pool": True,
                                "critical_reserve_breaches": []}
                    created.update(response)
                    append_event(conn, actor_id=actor_id, action=action,
                                 resource_type="application", resource_id=application_id,
                                 detail={"quantity": qty, "returned_to_reserve_pool": True},
                                 occurred_at=ts)
                    return "cargo_event", f"{voyage_id}:{application_id}:{event_type}", response
                self._apply_delta(conn, voyage, alloc, app, -qty, event_type,
                                  [{"code": reason_code, "detail": reason_text}], actor_id, ts,
                                  linked_to=None)
                releases.append({"application_id": application_id, "compartment": alloc["compartment"],
                                 "quantity": qty, "unit_weight": app["unit_weight"],
                                 "unit_volume": app["unit_volume"]})
                cascade = self._cascade(conn, voyage, releases, actor_id, ts)
                coverage = self._reserve_coverage(conn, voyage_id)
                breaches = [{"material_code": item["material_code"], "shortfall": item["shortfall"],
                             "required": item["required"], "covered": item["covered"]}
                            for item in coverage if not item["met"]]
                if breaches:
                    # 关键储备是越冬生存红线：任何留痕事件之后仍必须守恒。
                    # 无法由候补递补补齐时，本操作必须回滚，改走紧急双签预留通道。
                    raise ConflictError(
                        "本次释放经稳定递补后仍击穿关键储备，操作已回滚："
                        + canonical_json(breaches)
                        + "；请先追加同物料候补或走紧急预留双签批准")
                append_event(conn, actor_id=actor_id, action=action, resource_type="application",
                             resource_id=application_id,
                             detail={"quantity": qty, "reassigned": cascade,
                                     "reserve_breaches": []}, occurred_at=ts)
                response = {"voyage_id": voyage_id, "application_id": application_id,
                            "released_qty": qty, "reassigned": cascade,
                            "critical_reserve_breaches": []}
                created.update(response)
                return "cargo_event", f"{voyage_id}:{application_id}:{event_type}", response

            receipt = self._idempotent(conn, request_id=request_id, action=action,
                                       payload=payload, create=create)
            return {**created, "replayed": receipt.replayed}

    def report_flight_cancellation(self, *, request_id: str, actor_id: str,
                                   voyage_id: str, reason: str = "") -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id, reason=reason)
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            replay = self._peek_replay(conn, request_id=request_id, action="flight_cancellation",
                                       payload=payload)
            if replay is not None:
                return replay
            voyage = self._voyage(conn, voyage_id)
            if voyage["status"] != "frozen":
                raise ConflictError("航次未冻结")
            ts = self._now()
            deferred = []

            def create():
                rows = conn.execute(
                    "SELECT a.* FROM allocations a JOIN applications p ON p.application_id=a.application_id "
                    "WHERE a.voyage_id=? AND a.allocated_qty-a.loaded_qty>1e-9 AND p.is_emergency=0 "
                    "ORDER BY a.rank, a.application_id",
                    (voyage_id,)).fetchall()
                releases: list[dict[str, Any]] = []
                for alloc_row in rows:
                    alloc = row_dict(alloc_row)
                    qty = alloc["allocated_qty"] - alloc["loaded_qty"]
                    app = self._application(conn, voyage_id, alloc["application_id"])
                    relation = conn.execute(
                        "SELECT * FROM application_relations WHERE from_application_id=? AND kind='defer' "
                        "AND active=1 ORDER BY version DESC LIMIT 1", (alloc["application_id"],)).fetchone()
                    successor = relation["successor_voyage_id"] if relation else voyage_id
                    if successor != voyage_id:
                        # 已登记顺延关系且指向后续航次：份额确定性顺延
                        self._apply_delta(
                            conn, voyage, alloc, app, -qty, "deferred",
                            [{"code": "flight_cancelled",
                              "detail": reason_text_or("航班取消，未装载份额按顺延关系转出", reason),
                              "successor_voyage_id": successor}],
                            actor_id, ts, linked_to=None)
                        if alloc["loaded_qty"] <= EPS:
                            # 完全未装载才整体转为顺延；部分已装机的残留仍在本航次
                            conn.execute("UPDATE allocations SET state='deferred' WHERE allocation_id=?",
                                         (alloc["allocation_id"],))
                        conn.execute(
                            "UPDATE applications SET status='waitlisted', updated_at=? WHERE application_id=?",
                            (ts, alloc["application_id"]))
                        deferred.append({"application_id": alloc["application_id"], "quantity": round(qty, 9),
                                         "loaded_kept": round(alloc["loaded_qty"], 9),
                                         "successor_voyage_id": successor})
                    else:
                        # 无顺延去向：仅释放尚未装载和领用的份额，稍后按冻结排序稳定递补
                        self._apply_delta(
                            conn, voyage, alloc, app, -qty, "released",
                            [{"code": "flight_cancelled",
                              "detail": reason_text_or("航班取消，未装载份额释放待稳定转配", reason)}],
                            actor_id, ts, linked_to=None)
                        releases.append({"application_id": alloc["application_id"],
                                         "compartment": alloc["compartment"], "quantity": qty,
                                         "unit_weight": app["unit_weight"],
                                         "unit_volume": app["unit_volume"]})
                reassigned = self._cascade(conn, voyage, releases, actor_id, ts) if releases else []
                coverage = self._reserve_coverage(conn, voyage_id)
                breaches = [{"material_code": item["material_code"], "shortfall": item["shortfall"]}
                            for item in coverage if not item["met"]]
                if breaches:
                    raise ConflictError(
                        "航班取消处理会击穿关键储备（既无法递补也无顺延去向），操作已回滚："
                        + canonical_json(breaches))
                append_event(conn, actor_id=actor_id, action="cargo.flight_cancelled",
                             resource_type="voyage", resource_id=voyage_id,
                             detail={"deferred": deferred, "reassigned": reassigned, "reason": reason},
                             occurred_at=ts)
                return "flight_cancellation", voyage_id, {
                    "voyage_id": voyage_id, "deferred": deferred, "reassigned": reassigned}

            receipt = self._idempotent(conn, request_id=request_id, action="flight_cancellation",
                                       payload=payload, create=create)
            return {"voyage_id": voyage_id, "deferred_count": len(deferred),
                    "replayed": receipt.replayed}

    def swap_to_substitute(self, *, request_id: str, actor_id: str, voyage_id: str,
                           from_application_id: str, to_application_id: str,
                           quantity: float) -> dict[str, Any]:
        payload = locals_extra(request_id=request_id, actor_id=actor_id, voyage_id=voyage_id,
                               from_application_id=from_application_id,
                               to_application_id=to_application_id, quantity=quantity)
        qty = positive(quantity, "quantity")
        with self.database.transaction(immediate=True) as conn:
            actor = self._actor(conn, actor_id)
            self._require(actor, *WRITE_ROLES)
            replay = self._peek_replay(conn, request_id=request_id, action="swap_to_substitute",
                                       payload=payload)
            if replay is not None:
                return replay
            voyage = self._voyage(conn, voyage_id)
            if voyage["status"] != "frozen":
                raise ConflictError("航次未冻结")
            relation = conn.execute(
                "SELECT * FROM application_relations WHERE voyage_id=? AND from_application_id=? "
                "AND to_application_id=? AND kind='substitute' AND active=1 ORDER BY version DESC LIMIT 1",
                (voyage_id, from_application_id, to_application_id)).fetchone()
            if relation is None:
                raise NotFoundError("两份申报之间不存在生效的替代关系（或已被新版本取代）")
            source_alloc = self._allocation(conn, voyage_id, from_application_id)
            source_app = self._application(conn, voyage_id, from_application_id)
            target_alloc = self._allocation(conn, voyage_id, to_application_id)
            target_app = self._application(conn, voyage_id, to_application_id)
            if source_alloc["state"] not in {"allocated", "partial"}:
                raise ConflictError("源申报没有已获批可换装的份额")
            if target_alloc["state"] not in {"waitlisted", "allocated", "partial"}:
                raise ConflictError("替代申报已被拒绝，不能作为换装目标")
            target_headroom = target_app["quantity"] - target_alloc["allocated_qty"]
            if qty > target_headroom + EPS:
                raise ConflictError(f"替代申报可承接的份额只有 {round(target_headroom, 6)}")
            movable = source_alloc["allocated_qty"] - source_alloc["loaded_qty"]
            if qty > movable + EPS:
                raise ConflictError(f"源申报可换装的未装载份额只有 {round(movable, 6)}")
            target_comp = compartment_of(target_app["hazard_class"])
            add_weight = qty * target_app["unit_weight"]
            add_volume = qty * target_app["unit_volume"]
            drop_weight = qty * source_app["unit_weight"]
            drop_volume = qty * source_app["unit_volume"]
            usage = self._compartment_usage(conn, voyage_id)
            cap_w = voyage["dry_weight_capacity"] if target_comp == "dry" else voyage["hazmat_weight_capacity"]
            cap_v = voyage["dry_volume_capacity"] if target_comp == "dry" else voyage["hazmat_volume_capacity"]
            if source_alloc["compartment"] == target_comp:
                net_w = add_weight - drop_weight
                net_v = add_volume - drop_volume
                fit = usage[target_comp][0] + net_w <= cap_w + EPS and \
                      usage[target_comp][1] + net_v <= cap_v + EPS
            else:
                fit = usage[target_comp][0] + add_weight <= cap_w + EPS and \
                      usage[target_comp][1] + add_volume <= cap_v + EPS
            if not fit:
                raise ConflictError("换装后目标舱位重量或体积超限，不能换装")
            ts = self._now()

            def create():
                self._apply_delta(conn, voyage, source_alloc, source_app, -qty, "swapped",
                                  [{"code": "temporary_swap_out", "detail": "临时换装给已登记替代品",
                                    "to_application_id": to_application_id}],
                                  actor_id, ts, linked_to=to_application_id)
                self._apply_delta(conn, voyage, target_alloc, target_app, qty, "transferred_in",
                                  [{"code": "temporary_swap_in", "detail": "依据替代关系接收换装份额",
                                    "from_application_id": from_application_id}],
                                  actor_id, ts, linked_to=from_application_id)
                breaches = [item for item in self._reserve_coverage(conn, voyage_id) if not item["met"]]
                if breaches:
                    raise ConflictError(
                        "换装会击穿关键储备，操作已回滚："
                        + canonical_json([{"material_code": b["material_code"],
                                           "shortfall": b["shortfall"]} for b in breaches]))
                append_event(conn, actor_id=actor_id, action="cargo.swapped",
                             resource_type="relation", resource_id=relation["relation_id"],
                             detail={"from": from_application_id, "to": to_application_id,
                                     "quantity": qty, "relation_version": relation["version"]},
                             occurred_at=ts)
                return "swap", relation["relation_id"], {
                    "from_application_id": from_application_id, "to_application_id": to_application_id,
                    "quantity": qty, "relation_version": relation["version"]}

            receipt = self._idempotent(conn, request_id=request_id, action="swap_to_substitute",
                                       payload=payload, create=create)
            return {"from_application_id": from_application_id, "to_application_id": to_application_id,
                    "quantity": qty, "relation_id": relation["relation_id"],
                    "relation_version": relation["version"], "replayed": receipt.replayed}

    def _cascade(self, conn, voyage, releases, actor_id: str, ts: str) -> list[dict[str, Any]]:
        """按冻结排序把释放份额确定性地递补给同舱候补申报。"""

        voyage_id = voyage["voyage_id"]
        pool = {"dry": [0.0, 0.0], "hazmat": [0.0, 0.0]}
        source_by_comp: dict[str, str] = {}
        for release in releases:
            comp = release["compartment"]
            pool[comp][0] += release["quantity"] * release["unit_weight"]
            pool[comp][1] += release["quantity"] * release["unit_volume"]
            source_by_comp.setdefault(comp, release["application_id"])
        source_ids = {release["application_id"] for release in releases}
        reassigned = []
        waiting = conn.execute(
            "SELECT a.* FROM allocations a WHERE a.voyage_id=? AND a.state IN ('waitlisted','partial') "
            "ORDER BY a.rank, a.application_id", (voyage_id,)).fetchall()
        for waiting_row in waiting:
            target_alloc = row_dict(waiting_row)
            if target_alloc["application_id"] in source_ids:
                continue
            comp = target_alloc["compartment"]
            if pool[comp][0] <= EPS and pool[comp][1] <= EPS:
                continue
            app = self._application(conn, voyage_id, target_alloc["application_id"])
            headroom = app["quantity"] - target_alloc["allocated_qty"]
            if headroom <= EPS:
                continue
            by_w = pool[comp][0] / app["unit_weight"]
            by_v = pool[comp][1] / app["unit_volume"]
            qty = max(0.0, min(headroom, by_w, by_v))
            if qty <= EPS:
                continue
            source_id = source_by_comp[comp]
            self._apply_delta(conn, voyage, target_alloc, app, qty, "transferred_in",
                              [{"code": "cascade_promotion",
                                "detail": "依据冻结排序由释放份额稳定递补",
                                "from_application_id": source_id}],
                              actor_id, ts, linked_to=source_id)
            pool[comp][0] -= qty * app["unit_weight"]
            pool[comp][1] -= qty * app["unit_volume"]
            reassigned.append({"application_id": target_alloc["application_id"],
                               "quantity": round(qty, 9), "from_application_id": source_id,
                               "compartment": comp})
        return reassigned

    # ------------------------------------------------------------ 解释与守恒

    def explain_application(self, voyage_id: str, application_id: str) -> dict[str, Any]:
        conn = self.database.connection
        app_row = conn.execute("SELECT * FROM applications WHERE voyage_id=? AND application_id=?",
                               (voyage_id, application_id)).fetchone()
        if app_row is None:
            raise NotFoundError("申报不存在")
        app = row_dict(app_row)
        alloc_row = conn.execute("SELECT * FROM allocations WHERE voyage_id=? AND application_id=?",
                                 (voyage_id, application_id)).fetchone()
        revisions = [row_dict(r) for r in conn.execute(
            "SELECT version,change_reason,created_by,created_at FROM application_revisions "
            "WHERE application_id=? ORDER BY version", (application_id,))]
        relations = [row_dict(r) for r in conn.execute(
            "SELECT * FROM application_relations WHERE voyage_id=? AND "
            "(from_application_id=? OR to_application_id=?) ORDER BY created_at",
            (voyage_id, application_id, application_id))]
        ledger_rows = [row_dict(r) for r in conn.execute(
            "SELECT * FROM allocation_ledger WHERE voyage_id=? AND application_id=? ORDER BY created_at,ledger_id",
            (voyage_id, application_id))]
        for item in ledger_rows:
            item["reason"] = json.loads(item.pop("reason_json"))
        emergency = conn.execute("SELECT * FROM emergency_releases WHERE application_id=?",
                                 (application_id,)).fetchone()
        explanation = {
            "application": app,
            "decision_reason": json.loads(app["decision_reason_json"]),
            "revisions": revisions,
            "relations": relations,
            "allocation": row_dict(alloc_row) if alloc_row else None,
            "ledger": ledger_rows,
            "emergency_release": row_dict(emergency) if emergency else None,
        }
        if alloc_row:
            explanation["allocation"]["reasons"] = json.loads(alloc_row["reason_json"])
        return explanation

    def verify_conservation(self, voyage_id: str) -> dict[str, Any]:
        """证明舱位容量、台账余额、预留量与关键储备的守恒关系。"""

        conn = self.database.connection
        voyage = self._voyage(conn, voyage_id)
        allocs = [row_dict(r) for r in conn.execute(
            "SELECT * FROM allocations WHERE voyage_id=?", (voyage_id,))]
        apps = {r["application_id"]: row_dict(r) for r in conn.execute(
            "SELECT * FROM applications WHERE voyage_id=?", (voyage_id,))}
        checks: list[dict[str, Any]] = []
        ok = True

        def check(name: str, passed: bool, detail: Any) -> None:
            nonlocal ok
            ok = ok and passed
            checks.append({"name": name, "passed": bool(passed), "detail": detail})

        usage = {"dry": [0.0, 0.0], "hazmat": [0.0, 0.0]}
        emergency_usage = {"dry": [0.0, 0.0], "hazmat": [0.0, 0.0]}
        for alloc in allocs:
            ledger_sum = conn.execute(
                "SELECT COALESCE(SUM(delta_qty),0) AS s FROM allocation_ledger "
                "WHERE voyage_id=? AND application_id=? AND event_type IN ({})".format(
                    ",".join("?" for _ in ALLOCATION_LEDGER_EVENTS)),
                (voyage_id, alloc["application_id"], *sorted(ALLOCATION_LEDGER_EVENTS))
            ).fetchone()["s"]
            check(f"ledger_balance:{alloc['application_id']}",
                  abs(ledger_sum - alloc["allocated_qty"]) <= 1e-6,
                  {"ledger_sum": round(ledger_sum, 9), "allocated_qty": alloc["allocated_qty"]})
            check(f"loaded_within_allocated:{alloc['application_id']}",
                  alloc["loaded_qty"] <= alloc["allocated_qty"] + 1e-6,
                  {"loaded": alloc["loaded_qty"], "allocated": alloc["allocated_qty"]})
            check(f"issued_within_loaded:{alloc['application_id']}",
                  alloc["issued_qty"] <= alloc["loaded_qty"] + 1e-6,
                  {"issued": alloc["issued_qty"], "loaded": alloc["loaded_qty"]})
            if alloc["state"] == "deferred":
                continue
            emergency_qty = conn.execute(
                "SELECT COALESCE(SUM(delta_qty),0) AS s FROM allocation_ledger "
                "WHERE voyage_id=? AND application_id=? AND event_type IN "
                "('emergency_allocated','emergency_released')",
                (voyage_id, alloc["application_id"])).fetchone()["s"]
            emergency_qty = max(0.0, emergency_qty)
            if emergency_qty > 0:
                app = apps[alloc["application_id"]]
                emergency_usage[alloc["compartment"]][0] += emergency_qty * app["unit_weight"]
                emergency_usage[alloc["compartment"]][1] += emergency_qty * app["unit_volume"]
            normal_qty = alloc["allocated_qty"] - emergency_qty
            usage[alloc["compartment"]][0] += normal_qty * apps[alloc["application_id"]]["unit_weight"]
            usage[alloc["compartment"]][1] += normal_qty * apps[alloc["application_id"]]["unit_volume"]

        for comp in ("dry", "hazmat"):
            cap_w = voyage[f"{comp}_weight_capacity"]
            cap_v = voyage[f"{comp}_volume_capacity"]
            res_w = voyage[f"reserved_{comp}_weight"]
            res_v = voyage[f"reserved_{comp}_volume"]
            used_res_w = voyage[f"reserved_{comp}_weight_used"]
            used_res_v = voyage[f"reserved_{comp}_volume_used"]
            check(f"reserved_{comp}_weight_balance",
                  abs(emergency_usage[comp][0] - used_res_w) <= 1e-6,
                  {"emergency_allocated": round(emergency_usage[comp][0], 9),
                   "reserve_used": used_res_w})
            check(f"reserved_{comp}_volume_balance",
                  abs(emergency_usage[comp][1] - used_res_v) <= 1e-6,
                  {"emergency_allocated": round(emergency_usage[comp][1], 9),
                   "reserve_used": used_res_v})
            check(f"reserved_{comp}_weight_within_pool", used_res_w <= res_w + 1e-6,
                  {"used": used_res_w, "reserved": res_w})
            check(f"reserved_{comp}_volume_within_pool", used_res_v <= res_v + 1e-6,
                  {"used": used_res_v, "reserved": res_v})
            check(f"{comp}_weight_partition", usage[comp][0] <= cap_w - res_w + 1e-6,
                  {"ordinary_used": round(usage[comp][0], 9),
                   "contestable_capacity": round(cap_w - res_w, 9)})
            check(f"{comp}_volume_partition", usage[comp][1] <= cap_v - res_v + 1e-6,
                  {"ordinary_used": round(usage[comp][1], 9),
                   "contestable_capacity": round(cap_v - res_v, 9)})
            check(f"{comp}_weight_capacity",
                  usage[comp][0] + used_res_w <= cap_w + 1e-6,
                  {"ordinary_used": round(usage[comp][0], 9), "reserve_used": used_res_w,
                   "capacity": cap_w})
            check(f"{comp}_volume_capacity",
                  usage[comp][1] + used_res_v <= cap_v + 1e-6,
                  {"ordinary_used": round(usage[comp][1], 9), "reserve_used": used_res_v,
                   "capacity": cap_v})

        reserve_report = self._reserve_coverage(conn, voyage_id, allocs, apps)
        for item in reserve_report:
            check(f"critical_reserve:{item['material_code']}", item["met"],
                  {"required": item["required"], "covered": item["covered"],
                   "note": "关键储备破口只可能由到货短少、物资失效等已留痕事件造成，不属于守恒错误"})

        audit_ok, audit_count = self.verify_audit()
        check("audit_chain", audit_ok, {"events": audit_count})
        breaches = [item for item in reserve_report if not item["met"]]
        return {"voyage_id": voyage_id, "status": voyage["status"],
                "conserved": ok and audit_ok,
                "critical_reserves_met": len(breaches) == 0,
                "critical_reserve_breaches": breaches,
                "compartment_usage": {k: [round(v[0], 9), round(v[1], 9)] for k, v in usage.items()},
                "critical_reserves": reserve_report, "checks": checks}

    def _reserve_coverage(self, conn, voyage_id: str, allocs=None, apps=None) -> list[dict[str, Any]]:
        """按当前配载余额核算各关键储备的覆盖情况。"""

        if allocs is None:
            allocs = [row_dict(r) for r in conn.execute(
                "SELECT * FROM allocations WHERE voyage_id=?", (voyage_id,))]
        if apps is None:
            apps = {r["application_id"]: row_dict(r) for r in conn.execute(
                "SELECT * FROM applications WHERE voyage_id=?", (voyage_id,))}
        report = []
        for reserve in conn.execute("SELECT * FROM critical_reserves WHERE voyage_id=?", (voyage_id,)):
            contributors = []
            covered = 0.0
            for alloc in allocs:
                app = apps[alloc["application_id"]]
                if app["material_code"] == reserve["material_code"] and alloc["state"] != "deferred":
                    covered += alloc["allocated_qty"]
                    if alloc["allocated_qty"] > EPS:
                        contributors.append({"application_id": alloc["application_id"],
                                             "code": app["code"],
                                             "allocated_qty": alloc["allocated_qty"]})
            # 顺延到后续航次的份额从台账 deferred 事件取，并核对顺延关系指向别的航次。
            forwarded = 0.0
            for app in apps.values():
                if app["material_code"] != reserve["material_code"]:
                    continue
                deferred_out = conn.execute(
                    "SELECT COALESCE(SUM(-delta_qty),0) AS s FROM allocation_ledger "
                    "WHERE voyage_id=? AND application_id=? AND event_type='deferred'",
                    (voyage_id, app["application_id"])).fetchone()["s"]
                if deferred_out <= EPS:
                    continue
                relation = conn.execute(
                    "SELECT successor_voyage_id FROM application_relations "
                    "WHERE from_application_id=? AND kind='defer' AND active=1 "
                    "ORDER BY version DESC LIMIT 1", (app["application_id"],)).fetchone()
                if relation and relation["successor_voyage_id"] not in (None, voyage_id):
                    forwarded += deferred_out
            met = covered + forwarded + 1e-6 >= reserve["quantity"]
            report.append({"material_code": reserve["material_code"], "unit": reserve["unit"],
                           "required": reserve["quantity"], "covered": round(covered, 9),
                           "forwarded_to_successor": round(forwarded, 9),
                           "shortfall": round(max(0.0, reserve["quantity"] - covered - forwarded), 9),
                           "met": met, "contributors": contributors})
        return report

    # ------------------------------------------------------------------ 工具

    def _voyage(self, conn, voyage_id: str):
        row = conn.execute("SELECT * FROM voyages WHERE voyage_id=?", (voyage_id,)).fetchone()
        if row is None:
            raise NotFoundError("航次不存在")
        return row

    @staticmethod
    def _require_open(voyage) -> None:
        if voyage["status"] != "open":
            raise ConflictError("航次已冻结，申报基线不可再改")

    def _application(self, conn, voyage_id: str, application_id: str):
        row = conn.execute("SELECT * FROM applications WHERE voyage_id=? AND application_id=?",
                           (voyage_id, application_id)).fetchone()
        if row is None:
            raise NotFoundError("申报不存在")
        return row

    def _allocation(self, conn, voyage_id: str, application_id: str):
        row = conn.execute("SELECT * FROM allocations WHERE voyage_id=? AND application_id=?",
                           (voyage_id, application_id)).fetchone()
        if row is None:
            raise NotFoundError("该申报在当前冻结版本中没有配载记录")
        return row

    @staticmethod
    def _ledger(conn, *, voyage_id, application_id, compartment, event_type, delta_qty,
                balance_after, reason, created_by, created_at):
        conn.execute(
            "INSERT INTO allocation_ledger(ledger_id,voyage_id,freeze_version,application_id,"
            "compartment,event_type,delta_qty,balance_after,reason_json,created_by,created_at) "
            "VALUES(?, ?,1,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, voyage_id, application_id, compartment, event_type,
             round(delta_qty, 9), round(balance_after, 9), canonical_json(reason), created_by, created_at),
        )

    def _apply_delta(self, conn, voyage, alloc, app, delta, event_type, reason, actor_id, ts,
                     *, linked_to):
        new_qty = round(alloc["allocated_qty"] + delta, 9)
        if new_qty < -EPS:
            raise ConflictError("转配后份额不能为负")
        new_state = alloc["state"]
        if delta < 0 and new_qty <= EPS:
            if event_type == "deferred":
                new_state = "deferred"
            elif alloc["state"] == "waitlisted":
                new_state = "waitlisted"
            else:
                new_state = "released"
        elif delta < 0:
            new_state = "allocated" if new_qty + EPS >= app["quantity"] else "partial"
        if delta > 0:
            new_state = "allocated" if new_qty + EPS >= app["quantity"] else "partial"
            conn.execute(
                "UPDATE applications SET status='approved', decision_reason_json=?, updated_at=? "
                "WHERE application_id=?",
                (canonical_json(reason + [{"code": "frozen_order_cascade",
                                           "detail": "按冻结时排序递补"}]), ts, app["application_id"]))
        conn.execute(
            "UPDATE allocations SET allocated_qty=?, weight_qty=?, volume_qty=?, state=?, updated_at=? "
            "WHERE allocation_id=?",
            (new_qty, round(new_qty * app["unit_weight"], 9), round(new_qty * app["unit_volume"], 9),
             new_state, ts, alloc["allocation_id"]))
        self._ledger(conn, voyage_id=voyage["voyage_id"], application_id=app["application_id"],
                     compartment=alloc["compartment"], event_type=event_type, delta_qty=delta,
                     balance_after=new_qty, reason=reason, created_by=actor_id, created_at=ts,
                     )
        if linked_to:
            conn.execute(
                "UPDATE allocation_ledger SET linked_application_id=? WHERE ledger_id="
                "(SELECT ledger_id FROM allocation_ledger WHERE voyage_id=? AND application_id=? "
                "ORDER BY created_at DESC, ledger_id DESC LIMIT 1)",
                (linked_to, voyage["voyage_id"], app["application_id"]))

    def _compartment_usage(self, conn, voyage_id: str) -> dict[str, list[float]]:
        usage = {"dry": [0.0, 0.0], "hazmat": [0.0, 0.0]}
        for row in conn.execute(
                "SELECT compartment, weight_qty, volume_qty, state FROM allocations WHERE voyage_id=?",
                (voyage_id,)):
            if row["state"] == "deferred":
                continue
            usage[row["compartment"]][0] += row["weight_qty"]
            usage[row["compartment"]][1] += row["volume_qty"]
        return usage

    def _check_org_quota(self, conn, voyage_id, organization_id, material_code, quantity, *, added):
        row = conn.execute(
            "SELECT max_quantity, max_weight FROM org_quotas WHERE voyage_id=? AND organization_id=? "
            "AND material_code=?", (voyage_id, organization_id, material_code)).fetchone()
        if row is None:
            return
        existing = conn.execute(
            "SELECT COALESCE(SUM(quantity),0) AS s FROM applications WHERE voyage_id=? AND organization_id=? "
            "AND material_code=? AND status!='cancelled'",
            (voyage_id, organization_id, material_code)).fetchone()["s"]
        total = existing + added + quantity
        if row["max_quantity"] is not None and total > row["max_quantity"] + EPS:
            raise ConflictError(
                f"机构 {organization_id} 对物料 {material_code} 的申报总量 {round(total, 3)} 已超配额 "
                f"{row['max_quantity']}，拆单不能绕过机构上限")
        if row["max_weight"] is not None:
            material = conn.execute(
                "SELECT unit_weight FROM materials m JOIN voyages v ON v.site_id=m.site_id "
                "WHERE v.voyage_id=? AND m.code=?", (voyage_id, material_code)).fetchone()
            if material and total * material["unit_weight"] > row["max_weight"] + EPS:
                raise ConflictError(
                    f"机构 {organization_id} 对物料 {material_code} 的申报总重量已超重量配额")


def positive(value: float, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{field} 必须是正数")
    return float(value)


def non_negative(value: float, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ValidationError(f"{field} 不能为负数")
    return float(value)


def positive_dict(**values: float) -> dict[str, float]:
    return {key: positive(value, key) for key, value in values.items()}


def _priority(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 100:
        raise ValidationError("priority_score 必须是 0 到 100 的整数")
    return value


def locals_extra(**kwargs: Any) -> dict[str, Any]:
    return kwargs


def reason_text_or(default: str, value: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default
