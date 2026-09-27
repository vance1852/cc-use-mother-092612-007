"""在基础层 SQLite 之上登记协同台账的领域表。"""

from __future__ import annotations

import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS sched_participants (
    participant_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    role_type TEXT NOT NULL,
    phone TEXT NOT NULL,
    title TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sched_qualifications (
    qualification_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES sched_participants(participant_id),
    skill TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sched_availability (
    availability_id TEXT PRIMARY KEY,
    participant_id TEXT NOT NULL REFERENCES sched_participants(participant_id),
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sched_zones (
    zone_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sched_posts (
    post_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL REFERENCES sched_zones(zone_id),
    name TEXT NOT NULL,
    required_skill TEXT NOT NULL,
    min_staff INTEGER NOT NULL CHECK(min_staff >= 0),
    is_responsible INTEGER NOT NULL CHECK(is_responsible IN (0, 1)),
    necessary_fields_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sched_post_dependencies (
    post_id TEXT NOT NULL REFERENCES sched_posts(post_id),
    depends_on_post_id TEXT NOT NULL REFERENCES sched_posts(post_id),
    PRIMARY KEY(post_id, depends_on_post_id)
);
CREATE TABLE IF NOT EXISTS sched_shifts (
    shift_id TEXT PRIMARY KEY,
    zone_id TEXT NOT NULL REFERENCES sched_zones(zone_id),
    name TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sched_versions (
    version_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES sched_shifts(shift_id),
    version_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('confirmed', 'superseded')),
    source TEXT NOT NULL,
    confirmed_by TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    UNIQUE(shift_id, version_no)
);
CREATE TABLE IF NOT EXISTS sched_assignments (
    assignment_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL REFERENCES sched_versions(version_id),
    shift_id TEXT NOT NULL REFERENCES sched_shifts(shift_id),
    post_id TEXT NOT NULL REFERENCES sched_posts(post_id),
    participant_id TEXT NOT NULL REFERENCES sched_participants(participant_id),
    UNIQUE(version_id, post_id, participant_id)
);
CREATE INDEX IF NOT EXISTS idx_sched_assignments_shift ON sched_assignments(shift_id, participant_id);
CREATE TABLE IF NOT EXISTS sched_shortages (
    shortage_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES sched_shifts(shift_id),
    participant_id TEXT NOT NULL REFERENCES sched_participants(participant_id),
    kind TEXT NOT NULL CHECK(kind IN ('late', 'early_leave', 'absent')),
    expected_at TEXT,
    note TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open', 'resolved')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sched_proposals (
    proposal_id TEXT PRIMARY KEY,
    shortage_id TEXT NOT NULL REFERENCES sched_shortages(shortage_id),
    shift_id TEXT NOT NULL REFERENCES sched_shifts(shift_id),
    changes_json TEXT NOT NULL,
    impact_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'confirmed', 'superseded')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sched_dispatch_rejections (
    rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    shortage_id TEXT NOT NULL REFERENCES sched_shortages(shortage_id),
    participant_id TEXT NOT NULL,
    post_id TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sched_checkins (
    checkin_id TEXT PRIMARY KEY,
    shift_id TEXT NOT NULL REFERENCES sched_shifts(shift_id),
    participant_id TEXT NOT NULL REFERENCES sched_participants(participant_id),
    kind TEXT NOT NULL CHECK(kind IN ('checkin', 'late_receipt')),
    occurred_at TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(shift_id, participant_id, kind)
);
CREATE TABLE IF NOT EXISTS sched_takeovers (
    takeover_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    holder_id TEXT NOT NULL REFERENCES actors(actor_id),
    reason TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sched_handovers (
    handover_id TEXT PRIMARY KEY,
    takeover_id TEXT NOT NULL REFERENCES sched_takeovers(takeover_id),
    item TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'done')),
    completed_by TEXT,
    completed_at TEXT
);
CREATE TRIGGER IF NOT EXISTS sched_checkins_no_update
BEFORE UPDATE ON sched_checkins
BEGIN
    SELECT RAISE(ABORT, '签到事实不可回写');
END;
CREATE TRIGGER IF NOT EXISTS sched_checkins_no_delete
BEFORE DELETE ON sched_checkins
BEGIN
    SELECT RAISE(ABORT, '签到事实不可删除');
END;
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    """在既有数据库连接上创建协同台账表结构。"""

    connection.executescript(SCHEMA)
