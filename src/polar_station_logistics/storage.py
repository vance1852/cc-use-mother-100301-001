"""为越冬物资配额与装载决策项目提供建表与数据库打开辅助。

业务表复用基础服务的 organizations、actors、sites、request_receipts 与
audit_events，本模块只追加物流决策自己的状态表，保证与基础服务共库同事务。
"""

from __future__ import annotations

from pathlib import Path

from polar_station_foundation.storage import Database


LOGISTICS_SCHEMA = """
CREATE TABLE IF NOT EXISTS log_supply_items (
    item_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    hazard_level TEXT NOT NULL,
    unit_weight_kg REAL NOT NULL CHECK(unit_weight_kg > 0),
    unit_volume_m3 REAL NOT NULL CHECK(unit_volume_m3 > 0),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_supply_batches (
    batch_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES log_supply_items(item_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_flights (
    flight_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    code TEXT NOT NULL,
    status TEXT NOT NULL,
    cutoff_at TEXT NOT NULL,
    arrival_at TEXT NOT NULL,
    freeze_version INTEGER NOT NULL CHECK(freeze_version >= 0),
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, code)
);
CREATE TABLE IF NOT EXISTS log_cargo_holds (
    hold_id TEXT PRIMARY KEY,
    flight_id TEXT NOT NULL REFERENCES log_flights(flight_id),
    code TEXT NOT NULL,
    weight_capacity_kg REAL NOT NULL CHECK(weight_capacity_kg > 0),
    volume_capacity_m3 REAL NOT NULL CHECK(volume_capacity_m3 > 0),
    hazard_levels_json TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    UNIQUE(flight_id, code)
);
CREATE TABLE IF NOT EXISTS log_flight_reserves (
    flight_id TEXT NOT NULL REFERENCES log_flights(flight_id),
    category TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES log_supply_items(item_id),
    minimum_quantity INTEGER NOT NULL CHECK(minimum_quantity >= 0),
    reserved_quantity INTEGER NOT NULL CHECK(reserved_quantity >= 0),
    PRIMARY KEY(flight_id, category)
);
CREATE TABLE IF NOT EXISTS log_institution_quotas (
    flight_id TEXT NOT NULL REFERENCES log_flights(flight_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    max_weight_kg REAL NOT NULL CHECK(max_weight_kg > 0),
    max_volume_m3 REAL NOT NULL CHECK(max_volume_m3 > 0),
    PRIMARY KEY(flight_id, organization_id)
);
CREATE TABLE IF NOT EXISTS log_declarations (
    declaration_id TEXT PRIMARY KEY,
    flight_id TEXT NOT NULL REFERENCES log_flights(flight_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    item_id TEXT NOT NULL REFERENCES log_supply_items(item_id),
    batch_id TEXT REFERENCES log_supply_batches(batch_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    priority INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 5),
    status TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1)
);
CREATE TABLE IF NOT EXISTS log_declaration_relations (
    relation_id TEXT PRIMARY KEY,
    from_declaration_id TEXT NOT NULL REFERENCES log_declarations(declaration_id),
    to_declaration_id TEXT REFERENCES log_declarations(declaration_id),
    kind TEXT NOT NULL,
    target_flight_id TEXT REFERENCES log_flights(flight_id),
    note TEXT NOT NULL,
    relation_version INTEGER NOT NULL CHECK(relation_version >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_emergency_requests (
    emergency_id TEXT PRIMARY KEY,
    flight_id TEXT NOT NULL REFERENCES log_flights(flight_id),
    item_id TEXT NOT NULL REFERENCES log_supply_items(item_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    justification TEXT NOT NULL,
    status TEXT NOT NULL,
    approval_window_seconds INTEGER NOT NULL CHECK(approval_window_seconds > 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_allocations (
    allocation_id TEXT PRIMARY KEY,
    flight_id TEXT NOT NULL REFERENCES log_flights(flight_id),
    hold_id TEXT NOT NULL REFERENCES log_cargo_holds(hold_id),
    declaration_id TEXT REFERENCES log_declarations(declaration_id),
    emergency_id TEXT REFERENCES log_emergency_requests(emergency_id),
    category TEXT NOT NULL,
    origin TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    weight_kg REAL NOT NULL CHECK(weight_kg > 0),
    volume_m3 REAL NOT NULL CHECK(volume_m3 > 0),
    status TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((declaration_id IS NULL) <> (emergency_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_log_allocations_flight ON log_allocations(flight_id, status);
CREATE TABLE IF NOT EXISTS log_emergency_approvals (
    emergency_id TEXT NOT NULL REFERENCES log_emergency_requests(emergency_id),
    approver_id TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    PRIMARY KEY(emergency_id, approver_id)
);
CREATE TABLE IF NOT EXISTS log_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    flight_id TEXT NOT NULL REFERENCES log_flights(flight_id),
    freeze_version INTEGER NOT NULL,
    declaration_id TEXT NOT NULL REFERENCES log_declarations(declaration_id),
    status TEXT NOT NULL,
    approved_quantity INTEGER NOT NULL CHECK(approved_quantity >= 0),
    reason_code TEXT NOT NULL,
    reason_detail TEXT NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_log_decisions_declaration ON log_decisions(declaration_id);
CREATE TABLE IF NOT EXISTS log_flight_snapshots (
    flight_id TEXT NOT NULL REFERENCES log_flights(flight_id),
    freeze_version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(flight_id, freeze_version)
);
"""


def open_database(path: str | Path = ":memory:") -> Database:
    """打开（或创建）同时包含基础服务与物流决策表的数据库。"""

    database = Database(path)
    database.connection.executescript(LOGISTICS_SCHEMA)
    return database
