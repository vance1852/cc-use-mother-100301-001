"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS voyages (
    voyage_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    code TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    winter_end_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','frozen','closed')),
    freeze_version INTEGER NOT NULL DEFAULT 0 CHECK(freeze_version >= 0),
    frozen_at TEXT,
    dry_weight_capacity REAL NOT NULL CHECK(dry_weight_capacity >= 0),
    dry_volume_capacity REAL NOT NULL CHECK(dry_volume_capacity >= 0),
    hazmat_weight_capacity REAL NOT NULL CHECK(hazmat_weight_capacity >= 0),
    hazmat_volume_capacity REAL NOT NULL CHECK(hazmat_volume_capacity >= 0),
    reserved_dry_weight REAL NOT NULL DEFAULT 0 CHECK(reserved_dry_weight >= 0),
    reserved_dry_volume REAL NOT NULL DEFAULT 0 CHECK(reserved_dry_volume >= 0),
    reserved_hazmat_weight REAL NOT NULL DEFAULT 0 CHECK(reserved_hazmat_weight >= 0),
    reserved_hazmat_volume REAL NOT NULL DEFAULT 0 CHECK(reserved_hazmat_volume >= 0),
    reserved_dry_weight_used REAL NOT NULL DEFAULT 0 CHECK(reserved_dry_weight_used >= 0),
    reserved_dry_volume_used REAL NOT NULL DEFAULT 0 CHECK(reserved_dry_volume_used >= 0),
    reserved_hazmat_weight_used REAL NOT NULL DEFAULT 0 CHECK(reserved_hazmat_weight_used >= 0),
    reserved_hazmat_volume_used REAL NOT NULL DEFAULT 0 CHECK(reserved_hazmat_volume_used >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, code)
);
CREATE TABLE IF NOT EXISTS materials (
    material_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    hazard_class TEXT NOT NULL CHECK(hazard_class IN
        ('non_hazardous','flammable','corrosive','lithium_battery','oxidizer','toxic')),
    unit_weight REAL NOT NULL CHECK(unit_weight > 0),
    unit_volume REAL NOT NULL CHECK(unit_volume > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(site_id, code)
);
CREATE TABLE IF NOT EXISTS critical_reserves (
    voyage_id TEXT NOT NULL REFERENCES voyages(voyage_id),
    material_code TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(voyage_id, material_code)
);
CREATE TABLE IF NOT EXISTS org_quotas (
    voyage_id TEXT NOT NULL REFERENCES voyages(voyage_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    material_code TEXT NOT NULL,
    max_quantity REAL CHECK(max_quantity IS NULL OR max_quantity >= 0),
    max_weight REAL CHECK(max_weight IS NULL OR max_weight >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(voyage_id, organization_id, material_code)
);
CREATE TABLE IF NOT EXISTS applications (
    application_id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL REFERENCES voyages(voyage_id),
    code TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    material_code TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    unit_weight REAL NOT NULL CHECK(unit_weight > 0),
    unit_volume REAL NOT NULL CHECK(unit_volume > 0),
    hazard_class TEXT NOT NULL,
    batch_expiry_at TEXT,
    priority_score INTEGER NOT NULL CHECK(priority_score BETWEEN 0 AND 100),
    is_emergency INTEGER NOT NULL DEFAULT 0 CHECK(is_emergency IN (0,1)),
    status TEXT NOT NULL DEFAULT 'submitted'
        CHECK(status IN ('submitted','approved','waitlisted','rejected','cancelled')),
    decision_reason_json TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 1 CHECK(version >= 1),
    decided_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(voyage_id, code)
);
CREATE TABLE IF NOT EXISTS application_revisions (
    revision_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    version INTEGER NOT NULL CHECK(version >= 1),
    payload_json TEXT NOT NULL,
    change_reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(application_id, version)
);
CREATE TABLE IF NOT EXISTS application_relations (
    relation_id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL REFERENCES voyages(voyage_id),
    from_application_id TEXT NOT NULL REFERENCES applications(application_id),
    to_application_id TEXT REFERENCES applications(application_id),
    kind TEXT NOT NULL CHECK(kind IN ('substitute','split','defer')),
    quantity REAL CHECK(quantity IS NULL OR quantity >= 0),
    successor_voyage_id TEXT REFERENCES voyages(voyage_id),
    note TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL CHECK(version >= 1),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    supersedes_relation_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
    allocation_id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL REFERENCES voyages(voyage_id),
    freeze_version INTEGER NOT NULL CHECK(freeze_version >= 1),
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    compartment TEXT NOT NULL CHECK(compartment IN ('dry','hazmat')),
    allocated_qty REAL NOT NULL CHECK(allocated_qty >= 0),
    loaded_qty REAL NOT NULL DEFAULT 0 CHECK(loaded_qty >= 0),
    issued_qty REAL NOT NULL DEFAULT 0 CHECK(issued_qty >= 0),
    weight_qty REAL NOT NULL CHECK(weight_qty >= 0),
    volume_qty REAL NOT NULL CHECK(volume_qty >= 0),
    rank INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN
        ('allocated','partial','waitlisted','rejected','released','deferred','transferred')),
    reason_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL,
    UNIQUE(voyage_id, freeze_version, application_id)
);
CREATE TABLE IF NOT EXISTS allocation_ledger (
    ledger_id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    freeze_version INTEGER NOT NULL,
    application_id TEXT NOT NULL,
    compartment TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN
        ('allocated','partial','waitlisted','rejected','released',
         'transferred_in','transferred_out','deferred','loaded','issued',
         'expired','swapped','emergency_allocated','emergency_released')),
    delta_qty REAL NOT NULL,
    balance_after REAL NOT NULL,
    linked_application_id TEXT,
    reason_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loading_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    request_id TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emergency_releases (
    release_id TEXT PRIMARY KEY,
    voyage_id TEXT NOT NULL REFERENCES voyages(voyage_id),
    application_id TEXT NOT NULL REFERENCES applications(application_id),
    quantity REAL NOT NULL CHECK(quantity > 0),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','approved','rejected','expired','consumed')),
    expires_at TEXT NOT NULL,
    approval1_by TEXT REFERENCES actors(actor_id),
    approval1_at TEXT,
    approval2_by TEXT REFERENCES actors(actor_id),
    approval2_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finalized_at TEXT
);
CREATE TABLE IF NOT EXISTS freeze_snapshots (
    voyage_id TEXT NOT NULL,
    freeze_version INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(voyage_id, freeze_version)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
