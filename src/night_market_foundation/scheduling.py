"""第二届中医文化夜市跨专区协同排班台账。

在基础服务的稳定边界（角色权限、请求幂等、SQLite 事务、哈希串联审计）之上，
登记人员资质、可服务时间、岗位依赖与已确认班次版本，并提供缺员替岗、
签到事实、紧急接管与交接的完整规则：

- 缺员后生成多种替岗选择并说明各自影响，只有负责人或有效期内的紧急
  接管人确认某个完整方案后才更新排班；
- 资格过期、时间冲突或最低在岗数不足时，确认动作整体失败，不会留下
  半套变更；
- 班次开始后的签到事实只能追加，不能回写；重复签到与迟到回执保持幂等；
- 紧急接管登记原因、有效期限和交接事项，期限届满后自动失去调度权，
  服务重启后仍可接续尚未完成的交接；
- 各岗位只能读取履职必需的参与者信息；
- 接口可回答指定时刻的区域责任人、未覆盖依赖和拒绝调度的具体原因。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .audit import append_event, canonical_json, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import Actor, WriteReceipt
from .service import DomainService

PARTICIPANT_ROLES = frozenset({"doctor", "nurse", "volunteer", "logistics"})
SKILLS = frozenset({"consultation", "nursing", "guiding", "logistics", "escort", "supply", "order"})
CHECKIN_KINDS = frozenset({"checkin", "late", "early_leave"})
DISPATCH_ROLES = ("admin", "operator")
LEDGER_READ_ROLES = ("admin", "operator", "reviewer")

# 岗位履职视图：每个岗位只能读取其履职必需的参与者字段。
DUTY_VIEWS: dict[str, tuple[str, ...]] = {
    "escort": ("participant_id", "name", "role_type", "phone"),
    "supply": ("participant_id", "name", "role_type", "positions"),
    "order": ("participant_id", "name", "role_type"),
}

QUALIFICATION_MISSING = "QUALIFICATION_MISSING"
QUALIFICATION_EXPIRED = "QUALIFICATION_EXPIRED"
AVAILABILITY_MISSING = "AVAILABILITY_MISSING"
TIME_CONFLICT = "TIME_CONFLICT"
MIN_STAFFING = "MIN_STAFFING"
WINDOW_OUTSIDE_SHIFT = "WINDOW_OUTSIDE_SHIFT"
NO_CANDIDATE = "NO_CANDIDATE"

_REASON_TEXT = {
    QUALIFICATION_MISSING: "没有岗位要求的资质",
    QUALIFICATION_EXPIRED: "资质有效期不能覆盖服务窗口",
    AVAILABILITY_MISSING: "可服务时间不能覆盖服务窗口",
    TIME_CONFLICT: "与已确认的班次安排时间冲突",
    MIN_STAFFING: "岗位在岗人数将低于最低要求",
    WINDOW_OUTSIDE_SHIFT: "服务窗口超出班次时间范围",
    NO_CANDIDATE: "没有可评估的替岗候选人",
}


def _reason(code: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "message": _REASON_TEXT[code], **extra}


def _row_dict(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _overlaps(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    return a_start < b_end and b_start < a_end


def _intervals_cover(intervals: list[tuple[str, str]], start: str, end: str) -> bool:
    """判断区间并集是否完整覆盖 [start, end]。"""

    current = start
    for iv_start, iv_end in sorted(intervals):
        if iv_start > current:
            break
        if iv_end > current:
            current = iv_end
        if current >= end:
            return True
    return current >= end


def _staffing_shortfalls(start: str, end: str, min_staff: int,
                         intervals: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """找出 [start, end] 内在岗人数低于 min_staff 的时段。"""

    if min_staff <= 0:
        return []
    points = sorted({start, end} | {p for iv in intervals for p in iv if start < p < end})
    shortfalls = []
    for seg_start, seg_end in zip(points, points[1:]):
        count = sum(1 for iv_start, iv_end in intervals if iv_start <= seg_start and iv_end >= seg_end)
        if count < min_staff:
            shortfalls.append({"start_ts": seg_start, "end_ts": seg_end,
                               "required": min_staff, "actual": count})
    return shortfalls


class SchedulingService(DomainService):
    """在基础边界上实现协同台账的领域规则。"""

    # ---------- 基础助手 ----------

    def _now_ts(self) -> str:
        """返回秒级精度的标准化当前时间，保证字典序等于时间序。"""

        return self.clock.now().astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _ts(self, value: Any, field: str) -> str:
        """把输入归一化为秒级精度的 UTC ISO 时间。"""

        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 ISO 8601 时间") from exc
        if parsed.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _replay(self, connection, *, request_id: str, action: str,
                payload: dict[str, Any]) -> WriteReceipt | None:
        """在执行业务校验前先识别幂等重放，保证重试拿到原始回执。"""

        request_id = self._identifier(request_id, "request_id")
        row = connection.execute("SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    def _require_org_scope(self, connection, actor: Actor, site_id: str) -> None:
        if actor.role == "admin":
            return
        row = connection.execute("SELECT organization_id FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        if actor.organization_id != row["organization_id"]:
            raise PermissionDenied("不能操作其他组织的台账")

    def _participant_row(self, connection, participant_id: str):
        row = connection.execute(
            "SELECT * FROM participants WHERE participant_id=?", (participant_id,)).fetchone()
        if row is None:
            raise NotFoundError("参与者不存在")
        return row

    def _shift_row(self, connection, shift_id: str):
        row = connection.execute("SELECT * FROM shifts WHERE shift_id=?", (shift_id,)).fetchone()
        if row is None:
            raise NotFoundError("班次不存在")
        return row

    def _position_rows(self, connection, shift_id: str):
        return connection.execute(
            "SELECT * FROM positions WHERE shift_id=? ORDER BY rowid", (shift_id,)).fetchall()

    def _expire_takeovers(self, connection) -> None:
        """把已过有效期限的接管标记为失效，期限届满后自动失去调度权。"""

        now = self._now_ts()
        rows = connection.execute(
            "SELECT * FROM takeovers WHERE status='active' AND valid_until<?", (now,)).fetchall()
        for row in rows:
            connection.execute(
                "UPDATE takeovers SET status='expired' WHERE takeover_id=?", (row["takeover_id"],))
            append_event(connection, actor_id="system", action="takeover.expired",
                         resource_type="takeover", resource_id=row["takeover_id"],
                         detail={"valid_until": row["valid_until"]}, occurred_at=self._now())

    def _require_dispatch(self, connection, actor: Actor, shift) -> None:
        """校验调度权：负责人角色，或有效期内的紧急接管人。"""

        if actor.role == "admin":
            return
        if actor.role == "operator":
            self._require_org_scope(connection, actor, shift["site_id"])
            return
        now = self._now_ts()
        row = connection.execute(
            "SELECT * FROM takeovers WHERE shift_id=? AND grantee_actor_id=? AND status='active'",
            (shift["shift_id"], actor.actor_id)).fetchone()
        if row is not None and row["valid_from"] <= now <= row["valid_until"]:
            return
        raise PermissionDenied("当前没有该班次的调度权")

    def _require_read(self, connection, actor_id: str) -> Actor:
        actor = self._actor(connection, actor_id)
        self._require(actor, *LEDGER_READ_ROLES)
        return actor

    # ---------- 台账登记 ----------

    def register_participant(self, *, request_id: str, actor_id: str, site_id: str,
                             participant_id: str, name: str, role_type: str,
                             contact: dict[str, Any] | None = None) -> WriteReceipt:
        contact = contact or {}
        payload = {"actor_id": actor_id, "site_id": site_id, "participant_id": participant_id,
                   "name": name, "role_type": role_type, "contact": contact}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, request_id=request_id,
                                  action="register_participant", payload=payload)
            if replay:
                return replay
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            self._require_org_scope(connection, actor, site_id)
            participant_id = self._identifier(participant_id, "participant_id")
            name = self._text(name, "name", 80)
            if role_type not in PARTICIPANT_ROLES:
                raise ValidationError("role_type 不在允许范围内")
            if not isinstance(contact, dict):
                raise ValidationError("contact 必须是对象")
            contact_json = canonical_json(contact)
            if len(contact_json) > 500:
                raise ValidationError("contact 内容过长")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO participants(participant_id,site_id,name,role_type,contact_json,active,created_at) "
                        "VALUES(?,?,?,?,?,1,?)",
                        (participant_id, site_id, name, role_type, contact_json, self._now()))
                except Exception as exc:
                    raise ConflictError("参与者编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="participant.registered",
                             resource_type="participant", resource_id=participant_id,
                             detail={"site_id": site_id, "name": name, "role_type": role_type},
                             occurred_at=self._now())
                return "participant", participant_id, {"participant_id": participant_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_participant", payload=payload, create=create)

    def register_qualification(self, *, request_id: str, actor_id: str, participant_id: str,
                               qualification_id: str, skill: str, certificate_no: str,
                               valid_from: str, valid_until: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "participant_id": participant_id,
                   "qualification_id": qualification_id, "skill": skill,
                   "certificate_no": certificate_no, "valid_from": valid_from, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, request_id=request_id,
                                  action="register_qualification", payload=payload)
            if replay:
                return replay
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            participant = self._participant_row(connection, participant_id)
            self._require_org_scope(connection, actor, participant["site_id"])
            qualification_id = self._identifier(qualification_id, "qualification_id")
            if skill not in SKILLS:
                raise ValidationError("skill 不在允许范围内")
            certificate_no = self._text(certificate_no, "certificate_no", 80)
            valid_from = self._ts(valid_from, "valid_from")
            valid_until = self._ts(valid_until, "valid_until")
            if valid_from >= valid_until:
                raise ValidationError("资质有效期起止无效")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO qualifications(qualification_id,participant_id,skill,certificate_no,"
                        "valid_from,valid_until,created_at) VALUES(?,?,?,?,?,?,?)",
                        (qualification_id, participant_id, skill, certificate_no,
                         valid_from, valid_until, self._now()))
                except Exception as exc:
                    raise ConflictError("资质编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="qualification.registered",
                             resource_type="qualification", resource_id=qualification_id,
                             detail={"participant_id": participant_id, "skill": skill,
                                     "valid_from": valid_from, "valid_until": valid_until},
                             occurred_at=self._now())
                return "qualification", qualification_id, {"qualification_id": qualification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_qualification", payload=payload, create=create)

    def register_availability(self, *, request_id: str, actor_id: str, participant_id: str,
                              windows: list[dict[str, Any]]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "participant_id": participant_id, "windows": windows}
        if not isinstance(windows, list) or not windows:
            raise ValidationError("windows 必须是非空列表")
        normalized: list[tuple[str, str]] = []
        for index, window in enumerate(windows):
            if not isinstance(window, dict):
                raise ValidationError(f"windows[{index}] 必须是对象")
            start = self._ts(window.get("start"), f"windows[{index}].start")
            end = self._ts(window.get("end"), f"windows[{index}].end")
            if start >= end:
                raise ValidationError("可服务时间窗口起止无效")
            if (start, end) not in normalized:
                normalized.append((start, end))
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, request_id=request_id,
                                  action="register_availability", payload=payload)
            if replay:
                return replay
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            participant = self._participant_row(connection, participant_id)
            self._require_org_scope(connection, actor, participant["site_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                inserted = 0
                for start, end in normalized:
                    exists = connection.execute(
                        "SELECT 1 FROM availability_windows WHERE participant_id=? AND start_ts=? AND end_ts=?",
                        (participant_id, start, end)).fetchone()
                    if exists:
                        continue
                    connection.execute(
                        "INSERT INTO availability_windows(window_id,participant_id,start_ts,end_ts) VALUES(?,?,?,?)",
                        (uuid.uuid4().hex, participant_id, start, end))
                    inserted += 1
                append_event(connection, actor_id=actor_id, action="availability.registered",
                             resource_type="participant", resource_id=participant_id,
                             detail={"windows": inserted}, occurred_at=self._now())
                return "availability", participant_id, {"participant_id": participant_id, "windows": inserted}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_availability", payload=payload, create=create)

    def register_zone(self, *, request_id: str, actor_id: str, site_id: str,
                      zone_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "site_id": site_id, "zone_id": zone_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, request_id=request_id,
                                  action="register_zone", payload=payload)
            if replay:
                return replay
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            if connection.execute("SELECT 1 FROM sites WHERE site_id=?", (site_id,)).fetchone() is None:
                raise NotFoundError("场所不存在")
            self._require_org_scope(connection, actor, site_id)
            zone_id = self._identifier(zone_id, "zone_id")
            name = self._text(name, "name", 80)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO zones(zone_id,site_id,name,created_at) VALUES(?,?,?,?)",
                        (zone_id, site_id, name, self._now()))
                except Exception as exc:
                    raise ConflictError("专区编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="zone.registered",
                             resource_type="zone", resource_id=zone_id,
                             detail={"site_id": site_id, "name": name}, occurred_at=self._now())
                return "zone", zone_id, {"zone_id": zone_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_zone", payload=payload, create=create)

    def create_shift(self, *, request_id: str, actor_id: str, shift_id: str, zone_id: str,
                     start_ts: str, end_ts: str, positions: list[dict[str, Any]],
                     dependencies: list[dict[str, Any]] | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "shift_id": shift_id, "zone_id": zone_id,
                   "start_ts": start_ts, "end_ts": end_ts,
                   "positions": positions, "dependencies": dependencies or []}
        if not isinstance(positions, list) or not positions:
            raise ValidationError("positions 必须是非空列表")
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, request_id=request_id,
                                  action="create_shift", payload=payload)
            if replay:
                return replay
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            zone = connection.execute("SELECT * FROM zones WHERE zone_id=?", (zone_id,)).fetchone()
            if zone is None:
                raise NotFoundError("专区不存在")
            self._require_org_scope(connection, actor, zone["site_id"])
            shift_id = self._identifier(shift_id, "shift_id")
            start = self._ts(start_ts, "start_ts")
            end = self._ts(end_ts, "end_ts")
            if start >= end:
                raise ValidationError("班次起止时间无效")
            parsed_positions: list[dict[str, Any]] = []
            seen: set[str] = set()
            for index, raw in enumerate(positions):
                if not isinstance(raw, dict):
                    raise ValidationError(f"positions[{index}] 必须是对象")
                position_id = self._identifier(str(raw.get("position_id", "")),
                                               f"positions[{index}].position_id")
                if position_id in seen:
                    raise ValidationError("岗位编号重复")
                seen.add(position_id)
                title = self._text(str(raw.get("title", "")), f"positions[{index}].title", 80)
                if raw.get("skill") not in SKILLS:
                    raise ValidationError("岗位技能不在允许范围内")
                min_staff = raw.get("min_staff")
                if isinstance(min_staff, bool) or not isinstance(min_staff, int) or min_staff < 0:
                    raise ValidationError("min_staff 必须是非负整数")
                parsed_positions.append({
                    "position_id": position_id, "title": title, "skill": raw["skill"],
                    "min_staff": min_staff, "responsible": 1 if raw.get("responsible") else 0,
                })
            parsed_dependencies: list[dict[str, str]] = []
            for index, raw in enumerate(dependencies or []):
                if not isinstance(raw, dict):
                    raise ValidationError(f"dependencies[{index}] 必须是对象")
                position_id = str(raw.get("position_id", ""))
                requires_position_id = str(raw.get("requires_position_id", ""))
                if position_id not in seen or requires_position_id not in seen:
                    raise ValidationError("岗位依赖必须引用本班次的岗位")
                if position_id == requires_position_id:
                    raise ValidationError("岗位不能依赖自身")
                note = str(raw.get("note", "")).strip()
                if len(note) > 200:
                    raise ValidationError("note 不能超过 200 个字符")
                parsed_dependencies.append({"position_id": position_id,
                                            "requires_position_id": requires_position_id,
                                            "note": note})

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO shifts(shift_id,zone_id,site_id,start_ts,end_ts,version,created_at) "
                        "VALUES(?,?,?,?,?,1,?)",
                        (shift_id, zone_id, zone["site_id"], start, end, self._now()))
                except Exception as exc:
                    raise ConflictError("班次编号已经存在") from exc
                for position in parsed_positions:
                    try:
                        connection.execute(
                            "INSERT INTO positions(position_id,shift_id,title,skill,min_staff,responsible,created_at) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (position["position_id"], shift_id, position["title"], position["skill"],
                             position["min_staff"], position["responsible"], self._now()))
                    except Exception as exc:
                        raise ConflictError("岗位编号已经存在") from exc
                for dependency in parsed_dependencies:
                    connection.execute(
                        "INSERT INTO position_dependencies(dependency_id,shift_id,position_id,"
                        "requires_position_id,note) VALUES(?,?,?,?,?)",
                        (uuid.uuid4().hex, shift_id, dependency["position_id"],
                         dependency["requires_position_id"], dependency["note"]))
                shift = {"shift_id": shift_id, "start_ts": start, "end_ts": end}
                snapshot = self._snapshot(connection, shift, 1)
                connection.execute(
                    "INSERT INTO shift_revisions(shift_id,version,snapshot_json,created_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (shift_id, 1, canonical_json(snapshot), actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="shift.created",
                             resource_type="shift", resource_id=shift_id,
                             detail={"zone_id": zone_id, "start_ts": start, "end_ts": end,
                                     "positions": len(parsed_positions),
                                     "dependencies": len(parsed_dependencies)},
                             occurred_at=self._now())
                return "shift", shift_id, {"shift_id": shift_id, "version": 1}

            return self._idempotent(connection, request_id=request_id,
                                    action="create_shift", payload=payload, create=create)

    # ---------- 调度 ----------

    def create_assignment(self, *, request_id: str, actor_id: str, shift_id: str,
                          position_id: str, participant_id: str,
                          start_ts: str | None = None, end_ts: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "shift_id": shift_id, "position_id": position_id,
                   "participant_id": participant_id, "start_ts": start_ts, "end_ts": end_ts}
        reasons: list[dict[str, Any]] = []
        receipt: WriteReceipt | None = None
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, request_id=request_id,
                                  action="create_assignment", payload=payload)
            if replay:
                return replay
            actor = self._actor(connection, actor_id)
            self._expire_takeovers(connection)
            shift = self._shift_row(connection, shift_id)
            self._require_dispatch(connection, actor, shift)
            position = connection.execute(
                "SELECT * FROM positions WHERE position_id=? AND shift_id=?",
                (position_id, shift_id)).fetchone()
            if position is None:
                raise NotFoundError("岗位不存在")
            participant = self._participant_row(connection, participant_id)
            if not participant["active"]:
                raise ValidationError("参与者已停用")
            start = self._ts(start_ts, "start_ts") if start_ts else shift["start_ts"]
            end = self._ts(end_ts, "end_ts") if end_ts else shift["end_ts"]
            changes = [{"action": "add", "position_id": position_id, "participant_id": participant_id,
                        "start_ts": start, "end_ts": end}]
            reasons = self._validate_changes(connection, shift, changes)
            if reasons:
                self._log_dispatch(connection, shift_id=shift_id, plan_id=None,
                                   subject_id=participant_id, action="assign",
                                   result="rejected", reasons=reasons, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="dispatch.rejected",
                             resource_type="shift", resource_id=shift_id,
                             detail={"participant_id": participant_id, "position_id": position_id,
                                     "reasons": [item["code"] for item in reasons]},
                             occurred_at=self._now())
            else:
                def create() -> tuple[str, str, dict[str, Any]]:
                    assignment_id = uuid.uuid4().hex
                    version = shift["version"] + 1
                    connection.execute(
                        "INSERT INTO assignments(assignment_id,shift_id,position_id,participant_id,"
                        "start_ts,end_ts,state,plan_id,version,assigned_by,created_at) "
                        "VALUES(?,?,?,?,?,?,'confirmed',NULL,?,?,?)",
                        (assignment_id, shift_id, position_id, participant_id,
                         start, end, version, actor_id, self._now()))
                    new_version = self._bump_shift(connection, shift, actor_id)
                    self._log_dispatch(connection, shift_id=shift_id, plan_id=None,
                                       subject_id=participant_id, action="assign",
                                       result="confirmed", reasons=[], actor_id=actor_id)
                    append_event(connection, actor_id=actor_id, action="assignment.created",
                                 resource_type="shift", resource_id=shift_id,
                                 detail={"assignment_id": assignment_id, "position_id": position_id,
                                         "participant_id": participant_id, "start_ts": start,
                                         "end_ts": end, "version": new_version},
                                 occurred_at=self._now())
                    return "assignment", assignment_id, {"assignment_id": assignment_id,
                                                         "version": new_version}

                receipt = self._idempotent(connection, request_id=request_id,
                                           action="create_assignment", payload=payload, create=create)
        if reasons:
            raise ConflictError("调度被拒绝：" + "；".join(sorted({item["message"] for item in reasons})))
        return receipt

    def generate_replacement_plan(self, *, request_id: str, actor_id: str, shift_id: str,
                                  participant_id: str, reason: str,
                                  window_start: str | None = None,
                                  window_end: str | None = None) -> WriteReceipt:
        reason = self._text(reason, "reason", 200)
        payload = {"actor_id": actor_id, "shift_id": shift_id, "participant_id": participant_id,
                   "reason": reason, "window_start": window_start, "window_end": window_end}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, request_id=request_id,
                                  action="generate_replacement_plan", payload=payload)
            if replay:
                return replay
            actor = self._actor(connection, actor_id)
            self._expire_takeovers(connection)
            shift = self._shift_row(connection, shift_id)
            self._require_dispatch(connection, actor, shift)
            absent = self._participant_row(connection, participant_id)
            start = self._ts(window_start, "window_start") if window_start \
                else max(self._now_ts(), shift["start_ts"])
            end = self._ts(window_end, "window_end") if window_end else shift["end_ts"]
            if start < shift["start_ts"] or end > shift["end_ts"] or start >= end:
                raise ValidationError("替换窗口必须落在班次时间范围内")
            affected = connection.execute(
                "SELECT * FROM assignments WHERE shift_id=? AND participant_id=? AND state='confirmed' "
                "AND start_ts<? AND end_ts>? ORDER BY rowid",
                (shift_id, participant_id, end, start)).fetchall()
            if not affected:
                raise ValidationError("该人员在替换窗口内没有已确认的班次安排")
            for row in affected:
                if start > row["start_ts"] and end < row["end_ts"]:
                    raise ValidationError("替换窗口必须覆盖岗位区间的起点或终点")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan_id = uuid.uuid4().hex
                options, rejections = self._build_options(connection, shift, absent, affected, start, end)
                connection.execute(
                    "INSERT INTO replacement_plans(plan_id,shift_id,absent_participant_id,reason,"
                    "window_start,window_end,status,created_by,created_at) VALUES(?,?,?,?,?,?,'proposed',?,?)",
                    (plan_id, shift_id, participant_id, reason, start, end, actor_id, self._now()))
                for option in options:
                    connection.execute(
                        "INSERT INTO plan_options(option_id,plan_id,label,impact_json,changes_json) "
                        "VALUES(?,?,?,?,?)",
                        (option["option_id"], plan_id, option["label"],
                         canonical_json(option["impact"]), canonical_json(option["changes"])))
                for rejection in rejections:
                    self._log_dispatch(connection, shift_id=shift_id, plan_id=plan_id,
                                       subject_id=rejection.get("participant_id", participant_id),
                                       action="generate", result="rejected",
                                       reasons=[rejection], actor_id=actor_id)
                if not options and not rejections:
                    self._log_dispatch(connection, shift_id=shift_id, plan_id=plan_id,
                                       subject_id=participant_id, action="generate",
                                       result="rejected", reasons=[_reason(NO_CANDIDATE)],
                                       actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="plan.generated",
                             resource_type="replacement_plan", resource_id=plan_id,
                             detail={"shift_id": shift_id, "absent_participant_id": participant_id,
                                     "window_start": start, "window_end": end,
                                     "options": len(options)},
                             occurred_at=self._now())
                return "replacement_plan", plan_id, {"plan_id": plan_id, "options": len(options)}

            return self._idempotent(connection, request_id=request_id,
                                    action="generate_replacement_plan", payload=payload, create=create)

    def confirm_plan_option(self, *, request_id: str, actor_id: str,
                            plan_id: str, option_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "option_id": option_id}
        reasons: list[dict[str, Any]] = []
        receipt: WriteReceipt | None = None
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, request_id=request_id,
                                  action="confirm_plan_option", payload=payload)
            if replay:
                return replay
            actor = self._actor(connection, actor_id)
            self._expire_takeovers(connection)
            plan = connection.execute(
                "SELECT * FROM replacement_plans WHERE plan_id=?", (plan_id,)).fetchone()
            if plan is None:
                raise NotFoundError("替岗方案不存在")
            shift = self._shift_row(connection, plan["shift_id"])
            self._require_dispatch(connection, actor, shift)
            if plan["status"] != "proposed":
                raise ConflictError("替岗方案已处理")
            option = connection.execute(
                "SELECT * FROM plan_options WHERE option_id=? AND plan_id=?",
                (option_id, plan_id)).fetchone()
            if option is None:
                raise NotFoundError("替岗选择不存在")
            changes = json.loads(option["changes_json"])
            reasons = self._validate_changes(connection, shift, changes)
            if reasons:
                self._log_dispatch(connection, shift_id=shift["shift_id"], plan_id=plan_id,
                                   subject_id=option_id, action="confirm_option",
                                   result="rejected", reasons=reasons, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="dispatch.rejected",
                             resource_type="replacement_plan", resource_id=plan_id,
                             detail={"option_id": option_id,
                                     "reasons": [item["code"] for item in reasons]},
                             occurred_at=self._now())
            else:
                def create() -> tuple[str, str, dict[str, Any]]:
                    version = shift["version"] + 1
                    self._apply_changes(connection, shift["shift_id"], changes,
                                        plan_id=plan_id, actor_id=actor_id, version=version)
                    new_version = self._bump_shift(connection, shift, actor_id)
                    connection.execute(
                        "UPDATE replacement_plans SET status='confirmed', confirmed_by=?, "
                        "confirmed_at=?, confirmed_option_id=? WHERE plan_id=?",
                        (actor_id, self._now(), option_id, plan_id))
                    self._log_dispatch(connection, shift_id=shift["shift_id"], plan_id=plan_id,
                                       subject_id=option_id, action="confirm_option",
                                       result="confirmed", reasons=[], actor_id=actor_id)
                    append_event(connection, actor_id=actor_id, action="plan.confirmed",
                                 resource_type="replacement_plan", resource_id=plan_id,
                                 detail={"option_id": option_id, "version": new_version},
                                 occurred_at=self._now())
                    return "replacement_plan", plan_id, {"plan_id": plan_id,
                                                         "option_id": option_id,
                                                         "version": new_version}

                receipt = self._idempotent(connection, request_id=request_id,
                                           action="confirm_plan_option", payload=payload, create=create)
        if reasons:
            raise ConflictError("调度被拒绝：" + "；".join(sorted({item["message"] for item in reasons})))
        return receipt

    # ---------- 签到事实 ----------

    def record_checkin(self, *, request_id: str, actor_id: str, shift_id: str,
                       participant_id: str, kind: str, occurred_at: str,
                       note: str = "") -> WriteReceipt:
        """追加一条签到事实。事实只能追加，不能回写；重复事实保持幂等。"""

        if kind not in CHECKIN_KINDS:
            raise ValidationError("签到类型无效")
        payload = {"actor_id": actor_id, "shift_id": shift_id, "participant_id": participant_id,
                   "kind": kind, "occurred_at": occurred_at, "note": note}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, request_id=request_id,
                                  action="record_checkin", payload=payload)
            if replay:
                return replay
            actor = self._actor(connection, actor_id)
            self._expire_takeovers(connection)
            shift = self._shift_row(connection, shift_id)
            self._require_dispatch(connection, actor, shift)
            self._participant_row(connection, participant_id)
            occurred = self._ts(occurred_at, "occurred_at")
            note = str(note or "").strip()
            if len(note) > 200:
                raise ValidationError("note 不能超过 200 个字符")
            assigned = connection.execute(
                "SELECT 1 FROM assignments WHERE shift_id=? AND participant_id=? LIMIT 1",
                (shift_id, participant_id)).fetchone()
            if assigned is None:
                raise ValidationError("该人员在此班次没有岗位安排")

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM checkins WHERE shift_id=? AND participant_id=? AND kind=?",
                    (shift_id, participant_id, kind)).fetchone()
                if existing is not None:
                    return "checkin", existing["checkin_id"], {"checkin_id": existing["checkin_id"],
                                                               "deduplicated": True}
                checkin_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO checkins(checkin_id,shift_id,participant_id,kind,occurred_at,note,"
                    "recorded_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (checkin_id, shift_id, participant_id, kind, occurred, note,
                     actor_id, self._now()))
                append_event(connection, actor_id=actor_id, action="checkin.recorded",
                             resource_type="checkin", resource_id=checkin_id,
                             detail={"shift_id": shift_id, "participant_id": participant_id,
                                     "kind": kind, "occurred_at": occurred},
                             occurred_at=self._now())
                return "checkin", checkin_id, {"checkin_id": checkin_id, "deduplicated": False}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_checkin", payload=payload, create=create)

    # ---------- 紧急接管与交接 ----------

    def register_takeover(self, *, request_id: str, actor_id: str, takeover_id: str,
                          shift_id: str, grantee_actor_id: str, reason: str,
                          valid_until: str, handover_items: list[str]) -> WriteReceipt:
        payload = {"actor_id": actor_id, "takeover_id": takeover_id, "shift_id": shift_id,
                   "grantee_actor_id": grantee_actor_id, "reason": reason,
                   "valid_until": valid_until, "handover_items": handover_items}
        if not isinstance(handover_items, list) or not handover_items:
            raise ValidationError("handover_items 必须是非空列表")
        items = [self._text(item, "handover_items", 200) for item in handover_items]
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, request_id=request_id,
                                  action="register_takeover", payload=payload)
            if replay:
                return replay
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            self._expire_takeovers(connection)
            shift = self._shift_row(connection, shift_id)
            self._require_org_scope(connection, actor, shift["site_id"])
            self._actor(connection, grantee_actor_id)
            takeover_id = self._identifier(takeover_id, "takeover_id")
            reason = self._text(reason, "reason", 200)
            valid_from = self._now_ts()
            valid_until_ts = self._ts(valid_until, "valid_until")
            if valid_until_ts <= valid_from:
                raise ValidationError("有效期限必须晚于当前时间")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO takeovers(takeover_id,shift_id,grantee_actor_id,reason,"
                        "valid_from,valid_until,status,created_by,created_at) "
                        "VALUES(?,?,?,?,?,?,'active',?,?)",
                        (takeover_id, shift_id, grantee_actor_id, reason,
                         valid_from, valid_until_ts, actor_id, self._now()))
                except Exception as exc:
                    raise ConflictError("紧急接管编号已经存在") from exc
                for content in items:
                    connection.execute(
                        "INSERT INTO handover_items(item_id,takeover_id,content,state) VALUES(?,?,?,'pending')",
                        (uuid.uuid4().hex, takeover_id, content))
                append_event(connection, actor_id=actor_id, action="takeover.registered",
                             resource_type="takeover", resource_id=takeover_id,
                             detail={"shift_id": shift_id, "grantee_actor_id": grantee_actor_id,
                                     "valid_until": valid_until_ts, "handover_items": len(items)},
                             occurred_at=self._now())
                return "takeover", takeover_id, {"takeover_id": takeover_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_takeover", payload=payload, create=create)

    def complete_handover_item(self, *, request_id: str, actor_id: str,
                               takeover_id: str, item_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "takeover_id": takeover_id, "item_id": item_id}
        with self.database.transaction(immediate=True) as connection:
            replay = self._replay(connection, request_id=request_id,
                                  action="complete_handover_item", payload=payload)
            if replay:
                return replay
            actor = self._actor(connection, actor_id)
            takeover = connection.execute(
                "SELECT * FROM takeovers WHERE takeover_id=?", (takeover_id,)).fetchone()
            if takeover is None:
                raise NotFoundError("紧急接管记录不存在")
            if actor.actor_id != takeover["grantee_actor_id"] and actor.role not in DISPATCH_ROLES:
                raise PermissionDenied("只有接管人或负责人能登记交接完成")
            item = connection.execute(
                "SELECT * FROM handover_items WHERE item_id=? AND takeover_id=?",
                (item_id, takeover_id)).fetchone()
            if item is None:
                raise NotFoundError("交接事项不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                if item["state"] == "done":
                    return "handover_item", item_id, {"item_id": item_id, "deduplicated": True}
                connection.execute(
                    "UPDATE handover_items SET state='done', done_by=?, done_at=? WHERE item_id=?",
                    (actor_id, self._now(), item_id))
                remaining = connection.execute(
                    "SELECT COUNT(*) AS count FROM handover_items WHERE takeover_id=? AND state='pending'",
                    (takeover_id,)).fetchone()["count"]
                if remaining == 0:
                    connection.execute(
                        "UPDATE takeovers SET status='completed' WHERE takeover_id=?", (takeover_id,))
                append_event(connection, actor_id=actor_id, action="handover_item.completed",
                             resource_type="handover_item", resource_id=item_id,
                             detail={"takeover_id": takeover_id, "takeover_completed": remaining == 0},
                             occurred_at=self._now())
                return "handover_item", item_id, {"item_id": item_id, "deduplicated": False}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_handover_item", payload=payload, create=create)

    # ---------- 查询 ----------

    def get_shift(self, *, actor_id: str, shift_id: str) -> dict[str, Any]:
        connection = self.database.connection
        self._require_read(connection, actor_id)
        shift = self._shift_row(connection, shift_id)
        positions = []
        for prow in self._position_rows(connection, shift_id):
            intervals = [(row["start_ts"], row["end_ts"]) for row in connection.execute(
                "SELECT start_ts,end_ts FROM assignments WHERE position_id=? AND state='confirmed'",
                (prow["position_id"],))]
            shortfalls = _staffing_shortfalls(shift["start_ts"], shift["end_ts"],
                                              prow["min_staff"], intervals)
            positions.append({"position_id": prow["position_id"], "title": prow["title"],
                              "skill": prow["skill"], "min_staff": prow["min_staff"],
                              "responsible": bool(prow["responsible"]),
                              "confirmed_count": len(intervals),
                              "staffing_ok": not shortfalls, "shortfalls": shortfalls})
        dependencies = []
        for drow in connection.execute(
                "SELECT * FROM position_dependencies WHERE shift_id=? ORDER BY rowid", (shift_id,)):
            intervals = [(row["start_ts"], row["end_ts"]) for row in connection.execute(
                "SELECT start_ts,end_ts FROM assignments WHERE position_id=? AND state='confirmed'",
                (drow["requires_position_id"],))]
            dependencies.append({"dependency_id": drow["dependency_id"],
                                 "position_id": drow["position_id"],
                                 "requires_position_id": drow["requires_position_id"],
                                 "note": drow["note"],
                                 "covered": _intervals_cover(intervals, shift["start_ts"], shift["end_ts"])})
        assignments = []
        for row in connection.execute(
                "SELECT a.*, p.name AS participant_name FROM assignments a "
                "JOIN participants p ON p.participant_id=a.participant_id "
                "WHERE a.shift_id=? AND a.state='confirmed' ORDER BY a.rowid", (shift_id,)):
            assignments.append({"assignment_id": row["assignment_id"],
                                "position_id": row["position_id"],
                                "participant_id": row["participant_id"],
                                "name": row["participant_name"],
                                "start_ts": row["start_ts"], "end_ts": row["end_ts"],
                                "version": row["version"]})
        versions = [row["version"] for row in connection.execute(
            "SELECT version FROM shift_revisions WHERE shift_id=? ORDER BY version", (shift_id,))]
        return {"shift_id": shift["shift_id"], "zone_id": shift["zone_id"],
                "site_id": shift["site_id"], "start_ts": shift["start_ts"],
                "end_ts": shift["end_ts"], "version": shift["version"], "versions": versions,
                "positions": positions, "dependencies": dependencies, "assignments": assignments}

    def shift_revision(self, *, actor_id: str, shift_id: str, version: int) -> dict[str, Any]:
        connection = self.database.connection
        self._require_read(connection, actor_id)
        row = connection.execute(
            "SELECT * FROM shift_revisions WHERE shift_id=? AND version=?",
            (shift_id, version)).fetchone()
        if row is None:
            raise NotFoundError("班次版本不存在")
        return json.loads(row["snapshot_json"])

    def list_replacement_plans(self, *, actor_id: str, plan_id: str | None = None,
                               shift_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._require_read(connection, actor_id)
        if not plan_id and not shift_id:
            raise ValidationError("plan_id 与 shift_id 至少提供一个")
        query = "SELECT * FROM replacement_plans WHERE 1=1"
        parameters: list[Any] = []
        if plan_id:
            query += " AND plan_id=?"
            parameters.append(plan_id)
        if shift_id:
            query += " AND shift_id=?"
            parameters.append(shift_id)
        query += " ORDER BY created_at, plan_id"
        plans = []
        for row in connection.execute(query, parameters):
            options = []
            for orow in connection.execute(
                    "SELECT * FROM plan_options WHERE plan_id=? ORDER BY rowid", (row["plan_id"],)):
                options.append({"option_id": orow["option_id"], "label": orow["label"],
                                "impact": json.loads(orow["impact_json"]),
                                "changes": json.loads(orow["changes_json"])})
            plans.append({"plan_id": row["plan_id"], "shift_id": row["shift_id"],
                          "absent_participant_id": row["absent_participant_id"],
                          "reason": row["reason"], "window_start": row["window_start"],
                          "window_end": row["window_end"], "status": row["status"],
                          "created_by": row["created_by"], "created_at": row["created_at"],
                          "confirmed_by": row["confirmed_by"], "confirmed_at": row["confirmed_at"],
                          "confirmed_option_id": row["confirmed_option_id"], "options": options})
        return plans

    def list_checkins(self, *, actor_id: str, shift_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._require_read(connection, actor_id)
        self._shift_row(connection, shift_id)
        rows = connection.execute(
            "SELECT * FROM checkins WHERE shift_id=? ORDER BY occurred_at, rowid", (shift_id,))
        return [{"checkin_id": row["checkin_id"], "shift_id": row["shift_id"],
                 "participant_id": row["participant_id"], "kind": row["kind"],
                 "occurred_at": row["occurred_at"], "note": row["note"],
                 "recorded_by": row["recorded_by"], "created_at": row["created_at"]}
                for row in rows]

    def zone_responsible(self, *, actor_id: str, zone_id: str, at: str) -> dict[str, Any]:
        connection = self.database.connection
        self._require_read(connection, actor_id)
        if connection.execute("SELECT 1 FROM zones WHERE zone_id=?", (zone_id,)).fetchone() is None:
            raise NotFoundError("专区不存在")
        moment = self._ts(at, "at")
        shift = connection.execute(
            "SELECT * FROM shifts WHERE zone_id=? AND start_ts<=? AND end_ts>? "
            "ORDER BY start_ts DESC LIMIT 1", (zone_id, moment, moment)).fetchone()
        if shift is None:
            return {"zone_id": zone_id, "at": moment, "shift_id": None, "responsible": []}
        rows = connection.execute(
            "SELECT a.participant_id AS participant_id, p.name AS name, "
            "a.position_id AS position_id, pos.title AS title "
            "FROM assignments a "
            "JOIN participants p ON p.participant_id=a.participant_id "
            "JOIN positions pos ON pos.position_id=a.position_id "
            "WHERE a.shift_id=? AND a.state='confirmed' AND pos.responsible=1 "
            "AND a.start_ts<=? AND a.end_ts>? ORDER BY a.rowid",
            (shift["shift_id"], moment, moment)).fetchall()
        return {"zone_id": zone_id, "at": moment, "shift_id": shift["shift_id"],
                "responsible": [_row_dict(row) for row in rows]}

    def uncovered_dependencies(self, *, actor_id: str, shift_id: str) -> dict[str, Any]:
        connection = self.database.connection
        self._require_read(connection, actor_id)
        shift = self._shift_row(connection, shift_id)
        positions = {row["position_id"]: row for row in self._position_rows(connection, shift_id)}
        items = []
        for drow in connection.execute(
                "SELECT * FROM position_dependencies WHERE shift_id=? ORDER BY rowid", (shift_id,)):
            intervals = [(row["start_ts"], row["end_ts"]) for row in connection.execute(
                "SELECT start_ts,end_ts FROM assignments WHERE position_id=? AND state='confirmed'",
                (drow["requires_position_id"],))]
            covered = _intervals_cover(intervals, shift["start_ts"], shift["end_ts"])
            items.append({"dependency_id": drow["dependency_id"],
                          "position_id": drow["position_id"],
                          "position_title": positions[drow["position_id"]]["title"],
                          "requires_position_id": drow["requires_position_id"],
                          "requires_title": positions[drow["requires_position_id"]]["title"],
                          "note": drow["note"], "covered": covered})
        return {"shift_id": shift_id, "items": items,
                "uncovered": [item["dependency_id"] for item in items if not item["covered"]]}

    def dispatch_logs(self, *, actor_id: str, shift_id: str | None = None,
                      result: str | None = None) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._require_read(connection, actor_id)
        if result is not None and result not in ("confirmed", "rejected"):
            raise ValidationError("result 只能是 confirmed 或 rejected")
        query = "SELECT * FROM dispatch_logs WHERE 1=1"
        parameters: list[Any] = []
        if shift_id:
            query += " AND shift_id=?"
            parameters.append(shift_id)
        if result:
            query += " AND result=?"
            parameters.append(result)
        query += " ORDER BY rowid"
        return [{"log_id": row["log_id"], "shift_id": row["shift_id"], "plan_id": row["plan_id"],
                 "subject_id": row["subject_id"], "action": row["action"], "result": row["result"],
                 "reasons": json.loads(row["reasons_json"]), "created_by": row["created_by"],
                 "created_at": row["created_at"]}
                for row in connection.execute(query, parameters)]

    def list_takeovers(self, *, actor_id: str, shift_id: str) -> list[dict[str, Any]]:
        connection = self.database.connection
        self._require_read(connection, actor_id)
        now = self._now_ts()
        takeovers = []
        for row in connection.execute(
                "SELECT * FROM takeovers WHERE shift_id=? ORDER BY created_at, takeover_id",
                (shift_id,)):
            items = [{"item_id": irow["item_id"], "content": irow["content"],
                      "state": irow["state"], "done_by": irow["done_by"],
                      "done_at": irow["done_at"]}
                     for irow in connection.execute(
                         "SELECT * FROM handover_items WHERE takeover_id=? ORDER BY rowid",
                         (row["takeover_id"],))]
            status = row["status"]
            if status == "active" and row["valid_until"] < now:
                status = "expired"
            takeovers.append({"takeover_id": row["takeover_id"], "shift_id": row["shift_id"],
                              "grantee_actor_id": row["grantee_actor_id"], "reason": row["reason"],
                              "valid_from": row["valid_from"], "valid_until": row["valid_until"],
                              "status": status, "handover_items": items})
        return takeovers

    def pending_handovers(self, *, actor_id: str) -> list[dict[str, Any]]:
        """列出仍有未办结交接事项的接管记录，用于服务恢复后接续交接。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        if actor.role in DISPATCH_ROLES:
            rows = connection.execute("SELECT * FROM takeovers ORDER BY created_at, takeover_id")
        else:
            rows = connection.execute(
                "SELECT * FROM takeovers WHERE grantee_actor_id=? ORDER BY created_at, takeover_id",
                (actor_id,))
        now = self._now_ts()
        result = []
        for row in rows:
            pending = [{"item_id": irow["item_id"], "content": irow["content"]}
                       for irow in connection.execute(
                           "SELECT * FROM handover_items WHERE takeover_id=? AND state='pending' "
                           "ORDER BY rowid", (row["takeover_id"],))]
            if not pending:
                continue
            status = row["status"]
            if status == "active" and row["valid_until"] < now:
                status = "expired"
            result.append({"takeover_id": row["takeover_id"], "shift_id": row["shift_id"],
                           "grantee_actor_id": row["grantee_actor_id"], "reason": row["reason"],
                           "valid_from": row["valid_from"], "valid_until": row["valid_until"],
                           "status": status, "pending_items": pending})
        return result

    def participants_view(self, *, actor_id: str, shift_id: str,
                          view: str | None = None) -> list[dict[str, Any]]:
        """按岗位履职视图读取参与者信息，非负责人只能拿到履职必需字段。"""

        connection = self.database.connection
        actor = self._actor(connection, actor_id)
        self._shift_row(connection, shift_id)
        rows = connection.execute(
            "SELECT DISTINCT p.* FROM participants p "
            "JOIN assignments a ON a.participant_id=p.participant_id "
            "WHERE a.shift_id=? AND a.state='confirmed' ORDER BY p.participant_id",
            (shift_id,)).fetchall()
        position_titles: dict[str, list[str]] = {}
        for row in connection.execute(
                "SELECT a.participant_id AS participant_id, p.title AS title FROM assignments a "
                "JOIN positions p ON p.position_id=a.position_id "
                "WHERE a.shift_id=? AND a.state='confirmed' ORDER BY a.rowid", (shift_id,)):
            position_titles.setdefault(row["participant_id"], []).append(row["title"])
        if actor.role in DISPATCH_ROLES:
            items = []
            for row in rows:
                qualifications = [{"skill": qrow["skill"], "valid_until": qrow["valid_until"]}
                                  for qrow in connection.execute(
                                      "SELECT skill,valid_until FROM qualifications "
                                      "WHERE participant_id=? ORDER BY skill", (row["participant_id"],))]
                items.append({"participant_id": row["participant_id"], "name": row["name"],
                              "role_type": row["role_type"],
                              "contact": json.loads(row["contact_json"]),
                              "qualifications": qualifications,
                              "positions": position_titles.get(row["participant_id"], []),
                              "active": bool(row["active"])})
            return items
        if actor.role == "auditor":
            raise PermissionDenied("审计角色不能读取参与者信息")
        if view not in DUTY_VIEWS:
            raise ValidationError("必须指定有效的岗位履职视图")
        items = []
        for row in rows:
            contact = json.loads(row["contact_json"])
            item: dict[str, Any] = {}
            for field in DUTY_VIEWS[view]:
                if field == "phone":
                    item["phone"] = contact.get("phone")
                elif field == "positions":
                    item["positions"] = position_titles.get(row["participant_id"], [])
                else:
                    item[field] = row[field]
            items.append(item)
        return items

    # ---------- 内部规则 ----------

    def _qualification_block(self, connection, participant_id: str, skill: str,
                             start: str, end: str):
        """返回 (拒绝原因|None, 可用资质行|None)。"""

        rows = connection.execute(
            "SELECT * FROM qualifications WHERE participant_id=? AND skill=? "
            "ORDER BY valid_until DESC", (participant_id, skill)).fetchall()
        if not rows:
            return QUALIFICATION_MISSING, None
        for row in rows:
            if row["valid_from"] <= start and row["valid_until"] >= end:
                return None, row
        return QUALIFICATION_EXPIRED, None

    def _availability_ok(self, connection, participant_id: str, start: str, end: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM availability_windows WHERE participant_id=? AND start_ts<=? AND end_ts>=? "
            "LIMIT 1", (participant_id, start, end)).fetchone()
        return row is not None

    def _conflicting_assignments(self, connection, participant_id: str, start: str, end: str):
        return connection.execute(
            "SELECT * FROM assignments WHERE participant_id=? AND state='confirmed' "
            "AND start_ts<? AND end_ts>?", (participant_id, end, start)).fetchall()

    def _validate_changes(self, connection, shift, changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """模拟应用整组变更并返回拒绝原因；空列表表示可以整体提交。"""

        reasons: list[dict[str, Any]] = []
        positions = {row["position_id"]: row for row in self._position_rows(connection, shift["shift_id"])}
        confirmed = connection.execute(
            "SELECT * FROM assignments WHERE shift_id=? AND state='confirmed'",
            (shift["shift_id"],)).fetchall()
        removed: set[str] = set()
        shortened: dict[str, str] = {}
        adds: list[dict[str, Any]] = []
        for change in changes:
            action = change.get("action")
            if action == "release":
                removed.add(change["assignment_id"])
            elif action == "shorten":
                shortened[change["assignment_id"]] = change["end_ts"]
            elif action == "add":
                adds.append(change)
        for add in adds:
            participant_id = add["participant_id"]
            start, end = add["start_ts"], add["end_ts"]
            position = positions.get(add["position_id"])
            if position is None:
                reasons.append({"code": "POSITION_UNKNOWN", "message": "岗位不存在",
                                "participant_id": participant_id, "position_id": add["position_id"]})
                continue
            if not (shift["start_ts"] <= start < end <= shift["end_ts"]):
                reasons.append(_reason(WINDOW_OUTSIDE_SHIFT, participant_id=participant_id,
                                       position_id=position["position_id"]))
                continue
            code, _ = self._qualification_block(connection, participant_id,
                                                position["skill"], start, end)
            if code:
                reasons.append(_reason(code, participant_id=participant_id,
                                       position_id=position["position_id"]))
            if not self._availability_ok(connection, participant_id, start, end):
                reasons.append(_reason(AVAILABILITY_MISSING, participant_id=participant_id,
                                       position_id=position["position_id"]))
            for row in self._conflicting_assignments(connection, participant_id, start, end):
                if row["assignment_id"] in removed:
                    continue
                if row["assignment_id"] in shortened and shortened[row["assignment_id"]] <= start:
                    continue
                reasons.append(_reason(TIME_CONFLICT, participant_id=participant_id,
                                       position_id=position["position_id"]))
                break
            for other in adds:
                if other is add or other["participant_id"] != participant_id:
                    continue
                if _overlaps(start, end, other["start_ts"], other["end_ts"]):
                    reasons.append(_reason(TIME_CONFLICT, participant_id=participant_id,
                                           position_id=position["position_id"]))
                    break
        staffing: dict[str, list[tuple[str, str]]] = {position_id: [] for position_id in positions}
        for row in confirmed:
            if row["assignment_id"] in removed:
                continue
            staffing[row["position_id"]].append(
                (row["start_ts"], shortened.get(row["assignment_id"], row["end_ts"])))
        for add in adds:
            if add["position_id"] in staffing:
                staffing[add["position_id"]].append((add["start_ts"], add["end_ts"]))
        # 只校验本次变更触及的岗位：未触及岗位的既有缺口通过查询接口暴露，
        # 不应阻止其它岗位的合法调度（例如初始建班时逐岗添加人员）。
        touched = {row["position_id"] for row in confirmed
                   if row["assignment_id"] in removed or row["assignment_id"] in shortened}
        touched |= {add["position_id"] for add in adds}
        for position_id in touched:
            position = positions[position_id]
            gaps = _staffing_shortfalls(shift["start_ts"], shift["end_ts"],
                                        position["min_staff"], staffing[position_id])
            for gap in gaps[:1]:
                reasons.append({
                    "code": MIN_STAFFING,
                    "message": f"岗位「{position['title']}」在 {gap['start_ts']} 至 {gap['end_ts']} "
                               f"在岗 {gap['actual']} 人，低于最低要求 {gap['required']} 人",
                    "position_id": position_id,
                })
        return reasons

    def _candidates(self, connection, shift, position, absent_id: str,
                    need_start: str, need_end: str):
        """评估替岗候选人，返回 ((空闲候选人, 可借调候选人), 拒绝记录)。"""

        free: list[dict[str, Any]] = []
        borrow: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        rows = connection.execute(
            "SELECT * FROM participants WHERE active=1 AND participant_id!=? "
            "ORDER BY participant_id", (absent_id,)).fetchall()
        for row in rows:
            participant_id = row["participant_id"]
            name = row["name"]
            code, qualification = self._qualification_block(
                connection, participant_id, position["skill"], need_start, need_end)
            if code:
                rejections.append(_reason(code, participant_id=participant_id, name=name))
                continue
            if not self._availability_ok(connection, participant_id, need_start, need_end):
                rejections.append(_reason(AVAILABILITY_MISSING,
                                          participant_id=participant_id, name=name))
                continue
            conflicts = self._conflicting_assignments(connection, participant_id, need_start, need_end)
            if not conflicts:
                free.append({"participant_id": participant_id, "name": name,
                             "valid_until": qualification["valid_until"]})
                continue
            donor = conflicts[0]
            same_slot = (len(conflicts) == 1 and donor["shift_id"] == shift["shift_id"]
                         and donor["position_id"] != position["position_id"]
                         and donor["start_ts"] == need_start and donor["end_ts"] == need_end)
            if not same_slot:
                rejections.append(_reason(TIME_CONFLICT, participant_id=participant_id, name=name))
                continue
            source = connection.execute(
                "SELECT * FROM positions WHERE position_id=?", (donor["position_id"],)).fetchone()
            remaining = connection.execute(
                "SELECT start_ts,end_ts FROM assignments WHERE position_id=? AND state='confirmed' "
                "AND assignment_id!=?", (source["position_id"], donor["assignment_id"])).fetchall()
            if _staffing_shortfalls(shift["start_ts"], shift["end_ts"], source["min_staff"],
                                    [(item["start_ts"], item["end_ts"]) for item in remaining]):
                rejections.append(_reason(MIN_STAFFING, participant_id=participant_id, name=name))
                continue
            borrow.append({"participant_id": participant_id, "name": name,
                           "valid_until": qualification["valid_until"],
                           "borrow_assignment_id": donor["assignment_id"],
                           "borrow_position_id": source["position_id"],
                           "borrow_position_title": source["title"],
                           "source_min_staff": source["min_staff"],
                           "source_count": len(remaining) + 1})
        return (free, borrow), rejections

    def _original_changes(self, slot: dict[str, Any]) -> list[dict[str, Any]]:
        assignment = slot["assignment"]
        start, end = slot["need_start"], slot["need_end"]
        if start <= assignment["start_ts"] and end >= assignment["end_ts"]:
            return [{"action": "release", "assignment_id": assignment["assignment_id"]}]
        if end >= assignment["end_ts"]:
            return [{"action": "shorten", "assignment_id": assignment["assignment_id"],
                     "end_ts": start}]
        return []

    def _original_impacts(self, absent_name: str, slot: dict[str, Any]) -> list[str]:
        assignment = slot["assignment"]
        title = slot["position"]["title"]
        start, end = slot["need_start"], slot["need_end"]
        if start <= assignment["start_ts"] and end >= assignment["end_ts"]:
            return [f"解除{absent_name}在岗位「{title}」的班次安排"]
        if end >= assignment["end_ts"]:
            return [f"{absent_name}在岗位「{title}」的在岗截止时间调整为{start}"]
        return [f"{start}至{end}为临时顶岗，{absent_name}的原班次安排不变"]

    def _candidate_impacts(self, candidate: dict[str, Any], slot: dict[str, Any],
                           kind: str) -> list[str]:
        notes = []
        if kind == "borrow":
            notes.append(
                f"从岗位「{candidate['borrow_position_title']}」借调{candidate['name']}，"
                f"该岗位在岗人数由{candidate['source_count']}降为{candidate['source_count'] - 1}"
                f"（最低要求{candidate['source_min_staff']}）")
        else:
            notes.append(f"{candidate['name']}在替换窗口内没有其他班次安排，调岗无连带影响")
        valid_until = _parse_ts(candidate["valid_until"])
        need_end = _parse_ts(slot["need_end"])
        if valid_until - need_end < timedelta(hours=2):
            notes.append(f"{candidate['name']}的资质将于{candidate['valid_until']}到期，"
                         f"距覆盖结束不足2小时")
        return notes

    def _compose_option(self, absent, picks: list[tuple[dict[str, Any], str, dict[str, Any]]],
                        extra_impacts: list[str] | None = None,
                        label_suffix: str = "") -> dict[str, Any]:
        changes: list[dict[str, Any]] = []
        impacts: list[str] = []
        names = []
        for slot, kind, candidate in picks:
            changes.extend(self._original_changes(slot))
            impacts.extend(self._original_impacts(absent["name"], slot))
            if kind == "borrow":
                changes.append({"action": "release",
                                "assignment_id": candidate["borrow_assignment_id"]})
            changes.append({"action": "add", "position_id": slot["position"]["position_id"],
                            "participant_id": candidate["participant_id"],
                            "start_ts": slot["need_start"], "end_ts": slot["need_end"]})
            impacts.extend(self._candidate_impacts(candidate, slot, kind))
            names.append(candidate["name"])
        impacts.extend(extra_impacts or [])
        return {"option_id": uuid.uuid4().hex,
                "label": "、".join(names) + f"接替{absent['name']}" + label_suffix,
                "impact": impacts, "changes": changes}

    def _candidate_covers(self, connection, candidate: dict[str, Any],
                          slot: dict[str, Any], start: str, end: str) -> bool:
        code, _ = self._qualification_block(connection, candidate["participant_id"],
                                            slot["position"]["skill"], start, end)
        if code:
            return False
        return self._availability_ok(connection, candidate["participant_id"], start, end)

    def _split_option(self, connection, absent, slot: dict[str, Any]) -> dict[str, Any] | None:
        free = slot["free"]
        if len(free) < 2:
            return None
        start = _parse_ts(slot["need_start"])
        end = _parse_ts(slot["need_end"])
        mid = (start + (end - start) / 2).replace(microsecond=0)
        if mid <= start or mid >= end:
            return None
        mid_ts = mid.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        first = second = None
        for candidate in free:
            if self._candidate_covers(connection, candidate, slot, slot["need_start"], mid_ts):
                first = candidate
                break
        for candidate in free:
            if candidate is not first and self._candidate_covers(
                    connection, candidate, slot, mid_ts, slot["need_end"]):
                second = candidate
                break
        if first is None or second is None:
            return None
        changes = self._original_changes(slot)
        changes.append({"action": "add", "position_id": slot["position"]["position_id"],
                        "participant_id": first["participant_id"],
                        "start_ts": slot["need_start"], "end_ts": mid_ts})
        changes.append({"action": "add", "position_id": slot["position"]["position_id"],
                        "participant_id": second["participant_id"],
                        "start_ts": mid_ts, "end_ts": slot["need_end"]})
        impacts = self._original_impacts(absent["name"], slot)
        impacts.append(f"由{first['name']}、{second['name']}分段覆盖，交接点{mid_ts}")
        return {"option_id": uuid.uuid4().hex,
                "label": f"{first['name']}、{second['name']}分段接替{absent['name']}",
                "impact": impacts, "changes": changes}

    def _build_options(self, connection, shift, absent, affected,
                       window_start: str, window_end: str):
        positions = {row["position_id"]: row
                     for row in self._position_rows(connection, shift["shift_id"])}
        slots: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        for assignment in affected:
            position = positions[assignment["position_id"]]
            need_start = max(window_start, assignment["start_ts"])
            need_end = min(window_end, assignment["end_ts"])
            (free, borrow), rejected = self._candidates(
                connection, shift, position, absent["participant_id"], need_start, need_end)
            rejections.extend(rejected)
            slots.append({"assignment": _row_dict(assignment), "position": _row_dict(position),
                          "need_start": need_start, "need_end": need_end,
                          "free": free, "borrow": borrow})
        options: list[dict[str, Any]] = []
        if len(slots) == 1:
            slot = slots[0]
            for candidate in slot["free"][:3]:
                options.append(self._compose_option(absent, [(slot, "free", candidate)]))
            for candidate in slot["borrow"][:2]:
                options.append(self._compose_option(absent, [(slot, "borrow", candidate)]))
            split = self._split_option(connection, absent, slot)
            if split is not None:
                options.append(split)
        else:
            for index in range(3):
                picks: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
                used: set[str] = set()
                for slot in slots:
                    pool = [("free", candidate) for candidate in slot["free"]]
                    pool += [("borrow", candidate) for candidate in slot["borrow"]]
                    choice = None
                    for offset in range(len(pool)):
                        kind, candidate = pool[(index + offset) % len(pool)]
                        if candidate["participant_id"] not in used:
                            choice = (slot, kind, candidate)
                            break
                    if choice is None:
                        break
                    used.add(choice[2]["participant_id"])
                    picks.append(choice)
                if len(picks) == len(slots):
                    options.append(self._compose_option(absent, picks))
        return options, rejections

    def _apply_changes(self, connection, shift_id: str, changes: list[dict[str, Any]],
                       *, plan_id: str, actor_id: str, version: int) -> None:
        for change in changes:
            action = change["action"]
            if action == "release":
                connection.execute(
                    "UPDATE assignments SET state='released' WHERE assignment_id=?",
                    (change["assignment_id"],))
            elif action == "shorten":
                connection.execute(
                    "UPDATE assignments SET end_ts=? WHERE assignment_id=?",
                    (change["end_ts"], change["assignment_id"]))
            elif action == "add":
                connection.execute(
                    "INSERT INTO assignments(assignment_id,shift_id,position_id,participant_id,"
                    "start_ts,end_ts,state,plan_id,version,assigned_by,created_at) "
                    "VALUES(?,?,?,?,?,?,'confirmed',?,?,?,?)",
                    (uuid.uuid4().hex, shift_id, change["position_id"], change["participant_id"],
                     change["start_ts"], change["end_ts"], plan_id, version,
                     actor_id, self._now()))

    def _snapshot(self, connection, shift, version: int) -> dict[str, Any]:
        positions = [{"position_id": row["position_id"], "title": row["title"],
                      "skill": row["skill"], "min_staff": row["min_staff"],
                      "responsible": bool(row["responsible"])}
                     for row in self._position_rows(connection, shift["shift_id"])]
        assignments = [{"assignment_id": row["assignment_id"], "position_id": row["position_id"],
                        "participant_id": row["participant_id"],
                        "start_ts": row["start_ts"], "end_ts": row["end_ts"]}
                       for row in connection.execute(
                           "SELECT * FROM assignments WHERE shift_id=? AND state='confirmed' "
                           "ORDER BY rowid", (shift["shift_id"],))]
        return {"shift_id": shift["shift_id"], "version": version,
                "start_ts": shift["start_ts"], "end_ts": shift["end_ts"],
                "positions": positions, "assignments": assignments}

    def _bump_shift(self, connection, shift, actor_id: str) -> int:
        version = shift["version"] + 1
        connection.execute("UPDATE shifts SET version=? WHERE shift_id=?",
                           (version, shift["shift_id"]))
        snapshot = self._snapshot(connection, shift, version)
        connection.execute(
            "INSERT INTO shift_revisions(shift_id,version,snapshot_json,created_by,created_at) "
            "VALUES(?,?,?,?,?)",
            (shift["shift_id"], version, canonical_json(snapshot), actor_id, self._now()))
        return version

    def _log_dispatch(self, connection, *, shift_id: str | None, plan_id: str | None,
                      subject_id: str, action: str, result: str,
                      reasons: list[dict[str, Any]], actor_id: str) -> None:
        connection.execute(
            "INSERT INTO dispatch_logs(log_id,shift_id,plan_id,subject_id,action,result,"
            "reasons_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, shift_id, plan_id, subject_id, action, result,
             canonical_json(reasons), actor_id, self._now()))
