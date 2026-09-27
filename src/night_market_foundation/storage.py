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
CREATE TABLE IF NOT EXISTS participants (
    participant_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    role_type TEXT NOT NULL,
    contact_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS qualifications (
    qualification_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    skill TEXT NOT NULL,
    certificate_no TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS availability_windows (
    window_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    UNIQUE(participant_id, start_ts, end_ts)
);
CREATE TABLE IF NOT EXISTS zones (
    zone_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shifts (
    shift_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL REFERENCES zones(zone_id),
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    title TEXT NOT NULL,
    skill TEXT NOT NULL,
    min_staff INTEGER NOT NULL CHECK(min_staff >= 0),
    responsible INTEGER NOT NULL CHECK(responsible IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS position_dependencies (
    dependency_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    position_id TEXT NOT NULL REFERENCES positions(position_id),
    requires_position_id TEXT NOT NULL REFERENCES positions(position_id),
    note TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assignments (
    assignment_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    position_id TEXT NOT NULL REFERENCES positions(position_id),
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('confirmed', 'released')),
    plan_id TEXT,
    version INTEGER NOT NULL,
    assigned_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shift_revisions (
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    version INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(shift_id, version)
);
CREATE TABLE IF NOT EXISTS replacement_plans (
    plan_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    absent_participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    reason TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed', 'confirmed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT,
    confirmed_option_id TEXT
);
CREATE TABLE IF NOT EXISTS plan_options (
    option_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES replacement_plans(plan_id),
    label TEXT NOT NULL,
    impact_json TEXT NOT NULL,
    changes_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkins (
    checkin_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    participant_id TEXT NOT NULL REFERENCES participants(participant_id),
    kind TEXT NOT NULL CHECK(kind IN ('checkin', 'late', 'early_leave')),
    occurred_at TEXT NOT NULL,
    note TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(shift_id, participant_id, kind)
);
CREATE TABLE IF NOT EXISTS takeovers (
    takeover_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES shifts(shift_id),
    grantee_actor_id TEXT NOT NULL REFERENCES actors(actor_id),
    reason TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active', 'expired', 'completed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handover_items (
    item_id TEXT PRIMARY KEY,
    takeover_id TEXT NOT NULL REFERENCES takeovers(takeover_id),
    content TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'done')),
    done_by TEXT,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS dispatch_logs (
    log_id TEXT PRIMARY KEY,
    shift_id TEXT,
    plan_id TEXT,
    subject_id TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('confirmed', 'rejected')),
    reasons_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
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
