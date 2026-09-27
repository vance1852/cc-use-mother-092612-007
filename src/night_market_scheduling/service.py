"""实现中医文化夜市跨专区协同排班台账的领域规则。

台账在基础层（操作者、场所、幂等回执、审计链、事务）之上登记：
人员资质、可服务时间、岗位依赖、已确认班次版本、缺员替岗方案、
签到事实、紧急接管与交接事项。所有多步写入都在单个事务内完成，
校验失败不会留下半套变更。
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from night_market_foundation.audit import append_event, canonical_json, digest
from night_market_foundation.clock import Clock, SystemClock
from night_market_foundation.errors import (
    ConflictError,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from night_market_foundation.models import Actor, WriteReceipt
from night_market_foundation.storage import Database

from .models import (
    CHECKIN_KINDS,
    PARTICIPANT_ROLES,
    PUBLIC_PARTICIPANT_FIELDS,
    REASON_AVAILABILITY_INSUFFICIENT,
    REASON_DEPENDENCY_UNCOVERED,
    REASON_DUPLICATE_IN_PLAN,
    REASON_MIN_STAFF_SHORTAGE,
    REASON_PARTICIPANT_INACTIVE,
    REASON_QUALIFICATION_EXPIRED,
    REASON_QUALIFICATION_MISSING,
    REASON_TEXT,
    REASON_TIME_CONFLICT,
    RESTRICTED_PARTICIPANT_FIELDS,
    SHORTAGE_KINDS,
    SKILLS,
    PlanIssue,
)
from .storage import ensure_schema


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
MANAGE_ROLES = frozenset({"admin", "operator"})
READ_ROLES = frozenset({"admin", "operator", "reviewer"})


class PlanRejected(ConflictError):
    """完整排班方案校验未通过，携带结构化的拒绝原因。"""

    def __init__(self, issues: list[PlanIssue]) -> None:
        self.issues = [
            {"reason": issue.reason, "message": issue.message,
             "post_id": issue.post_id, "participant_id": issue.participant_id}
            for issue in issues
        ]
        summary = "；".join(dict.fromkeys(issue.message for issue in issues))
        super().__init__(f"排班方案校验未通过：{summary}")


class SchedulingService:
    """协调协同台账的权限、幂等、事务、版本与审计规则。"""

    def __init__(self, database: Database, clock: Clock | None = None) -> None:
        ensure_schema(database.connection)
        self.database = database
        self.clock = clock or SystemClock()

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return self.clock.now().astimezone(timezone.utc)

    def _now_text(self) -> str:
        return self._fmt(self._now())

    def _fmt(self, moment: datetime) -> str:
        return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _parse_ts(self, value: str, field: str) -> datetime:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValidationError(f"{field} 时间格式无效") from exc
        if moment.tzinfo is None:
            raise ValidationError(f"{field} 必须包含时区")
        return moment.astimezone(timezone.utc)

    def _ts_text(self, value: str, field: str) -> str:
        return self._fmt(self._parse_ts(value, field))

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

    def _optional_text(self, value: Any, field: str, limit: int = 200) -> str:
        value = str(value or "").strip()
        if len(value) > limit:
            raise ValidationError(f"{field} 不能超过 {limit} 个字符")
        return value

    def _actor(self, connection, actor_id: str) -> Actor:
        row = connection.execute("SELECT * FROM actors WHERE actor_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFoundError("操作者不存在")
        actor = Actor(row["actor_id"], row["display_name"], row["role"],
                      row["organization_id"], bool(row["active"]))
        if not actor.active:
            raise PermissionDenied("操作者已停用")
        return actor

    def _site(self, connection, site_id: str):
        row = connection.execute("SELECT * FROM sites WHERE site_id=?", (site_id,)).fetchone()
        if row is None:
            raise NotFoundError("场所不存在")
        return row

    def _manage_authority(self, actor: Actor, organization_id: str) -> None:
        if actor.role == "admin":
            return
        if actor.role == "operator" and actor.organization_id == organization_id:
            return
        raise PermissionDenied("当前角色不能管理台账资料")

    def _read_authority(self, actor: Actor, organization_id: str) -> None:
        if actor.role == "admin":
            return
        if actor.role in ("operator", "reviewer") and actor.organization_id == organization_id:
            return
        raise PermissionDenied("当前角色不能读取台账资料")

    def _dispatch_authority(self, connection, actor: Actor, site_id: str) -> None:
        """调度权：管理员、本机构负责人，或期限内的紧急接管人。"""

        site = self._site(connection, site_id)
        if actor.role == "admin":
            return
        if actor.role == "operator" and actor.organization_id == site["organization_id"]:
            return
        now_text = self._now_text()
        row = connection.execute(
            "SELECT takeover_id FROM sched_takeovers WHERE site_id=? AND holder_id=? "
            "AND valid_from<=? AND valid_until>? ORDER BY valid_until DESC LIMIT 1",
            (site_id, actor.actor_id, now_text, now_text),
        ).fetchone()
        if row:
            return
        raise PermissionDenied("当前操作者没有调度权或接管期限已届满")

    def _receipt_replay(self, connection, *, request_id: str, action: str,
                        payload: dict[str, Any]) -> WriteReceipt | None:
        """若同一 request_id 已成功处理过，返回原回执；内容不同则报冲突。"""

        request_id = self._identifier(request_id, "request_id")
        row = connection.execute(
            "SELECT * FROM request_receipts WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["action"] != action or row["payload_hash"] != digest(payload):
            raise ConflictError("request_id 已被不同内容使用")
        return WriteReceipt(request_id, row["resource_type"], row["resource_id"], True)

    def _receipt_store(self, connection, *, request_id: str, action: str,
                       payload: dict[str, Any], resource_type: str, resource_id: str,
                       response: dict[str, Any]) -> None:
        request_id = self._identifier(request_id, "request_id")
        connection.execute(
            "INSERT INTO request_receipts(request_id,action,payload_hash,resource_type,resource_id,response_json,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (request_id, action, digest(payload), resource_type, resource_id,
             canonical_json(response), self._now_text()),
        )

    def _idempotent(self, connection, *, request_id: str, action: str,
                    payload: dict[str, Any],
                    create: Callable[[], tuple[str, str, dict[str, Any]]],
                    find_existing: Callable[[], tuple[str, str, dict[str, Any]] | None] | None = None
                    ) -> WriteReceipt:
        """与基础层一致的请求幂等；find_existing 支持业务键自然幂等。"""

        replay = self._receipt_replay(connection, request_id=request_id,
                                      action=action, payload=payload)
        if replay is not None:
            return replay
        replayed = False
        existing = find_existing() if find_existing else None
        if existing is not None:
            resource_type, resource_id, response = existing
            replayed = True
        else:
            resource_type, resource_id, response = create()
        self._receipt_store(connection, request_id=request_id, action=action, payload=payload,
                            resource_type=resource_type, resource_id=resource_id,
                            response=response)
        return WriteReceipt(self._identifier(request_id, "request_id"),
                            resource_type, resource_id, replayed)

    # ------------------------------------------------------------------
    # 台账资料登记
    # ------------------------------------------------------------------

    def register_participant(self, *, request_id: str, actor_id: str, participant_id: str,
                             site_id: str, name: str, role_type: str,
                             phone: str = "", title: str = "",
                             profile: dict[str, Any] | None = None) -> WriteReceipt:
        profile = profile or {}
        if not isinstance(profile, dict):
            raise ValidationError("profile 必须是对象")
        payload = {"actor_id": actor_id, "participant_id": participant_id, "site_id": site_id,
                   "name": name, "role_type": role_type, "phone": phone,
                   "title": title, "profile": profile}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            site = self._site(connection, site_id)
            self._manage_authority(actor, site["organization_id"])
            participant_id = self._identifier(participant_id, "participant_id")
            name = self._text(name, "name")
            if role_type not in PARTICIPANT_ROLES:
                raise ValidationError("role_type 不在允许范围内")
            phone = self._optional_text(phone, "phone", 40)
            title = self._optional_text(title, "title", 80)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO sched_participants(participant_id,site_id,name,role_type,phone,title,"
                        "profile_json,active,created_at) VALUES(?,?,?,?,?,?,?,1,?)",
                        (participant_id, site_id, name, role_type, phone, title,
                         canonical_json(profile), self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("参与者编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="participant.registered",
                             resource_type="participant", resource_id=participant_id,
                             detail={"site_id": site_id, "name": name, "role_type": role_type},
                             occurred_at=self._now_text())
                return "participant", participant_id, {"participant_id": participant_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_participant", payload=payload, create=create)

    def add_qualification(self, *, request_id: str, actor_id: str, participant_id: str,
                          skill: str, valid_from: str, valid_until: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "participant_id": participant_id, "skill": skill,
                   "valid_from": valid_from, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            participant = self._participant(connection, participant_id)
            self._manage_authority(actor, self._site(connection, participant["site_id"])["organization_id"])
            if skill not in SKILLS:
                raise ValidationError("skill 不在允许范围内")
            start = self._parse_ts(valid_from, "valid_from")
            end = self._parse_ts(valid_until, "valid_until")
            if not start < end:
                raise ValidationError("valid_from 必须早于 valid_until")
            valid_from_text, valid_until_text = self._fmt(start), self._fmt(end)

            def create() -> tuple[str, str, dict[str, Any]]:
                qualification_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO sched_qualifications(qualification_id,participant_id,skill,valid_from,"
                    "valid_until,created_at) VALUES(?,?,?,?,?,?)",
                    (qualification_id, participant_id, skill, valid_from_text,
                     valid_until_text, self._now_text()),
                )
                append_event(connection, actor_id=actor_id, action="qualification.added",
                             resource_type="participant", resource_id=participant_id,
                             detail={"skill": skill, "valid_from": valid_from_text,
                                     "valid_until": valid_until_text},
                             occurred_at=self._now_text())
                return "qualification", qualification_id, {"qualification_id": qualification_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_qualification", payload=payload, create=create)

    def add_availability(self, *, request_id: str, actor_id: str, participant_id: str,
                         start_at: str, end_at: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "participant_id": participant_id,
                   "start_at": start_at, "end_at": end_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            participant = self._participant(connection, participant_id)
            self._manage_authority(actor, self._site(connection, participant["site_id"])["organization_id"])
            start = self._parse_ts(start_at, "start_at")
            end = self._parse_ts(end_at, "end_at")
            if not start < end:
                raise ValidationError("start_at 必须早于 end_at")
            start_text, end_text = self._fmt(start), self._fmt(end)

            def create() -> tuple[str, str, dict[str, Any]]:
                availability_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO sched_availability(availability_id,participant_id,start_at,end_at) "
                    "VALUES(?,?,?,?)",
                    (availability_id, participant_id, start_text, end_text),
                )
                append_event(connection, actor_id=actor_id, action="availability.added",
                             resource_type="participant", resource_id=participant_id,
                             detail={"start_at": start_text, "end_at": end_text},
                             occurred_at=self._now_text())
                return "availability", availability_id, {"availability_id": availability_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_availability", payload=payload, create=create)

    def register_zone(self, *, request_id: str, actor_id: str, zone_id: str,
                      site_id: str, name: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "zone_id": zone_id, "site_id": site_id, "name": name}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            site = self._site(connection, site_id)
            self._manage_authority(actor, site["organization_id"])
            zone_id = self._identifier(zone_id, "zone_id")
            name = self._text(name, "name")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO sched_zones(zone_id,site_id,name,created_at) VALUES(?,?,?,?)",
                        (zone_id, site_id, name, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("专区编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="zone.registered",
                             resource_type="zone", resource_id=zone_id,
                             detail={"site_id": site_id, "name": name},
                             occurred_at=self._now_text())
                return "zone", zone_id, {"zone_id": zone_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_zone", payload=payload, create=create)

    def register_post(self, *, request_id: str, actor_id: str, post_id: str, zone_id: str,
                      name: str, required_skill: str, min_staff: int,
                      is_responsible: bool = False,
                      necessary_fields: list[str] | tuple[str, ...] = ()) -> WriteReceipt:
        necessary_fields = list(necessary_fields or [])
        payload = {"actor_id": actor_id, "post_id": post_id, "zone_id": zone_id, "name": name,
                   "required_skill": required_skill, "min_staff": min_staff,
                   "is_responsible": bool(is_responsible), "necessary_fields": necessary_fields}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            zone = self._zone(connection, zone_id)
            self._manage_authority(actor, self._site(connection, zone["site_id"])["organization_id"])
            post_id = self._identifier(post_id, "post_id")
            name = self._text(name, "name")
            if required_skill not in SKILLS:
                raise ValidationError("required_skill 不在允许范围内")
            if isinstance(min_staff, bool) or not isinstance(min_staff, int) or min_staff < 0:
                raise ValidationError("min_staff 必须是非负整数")
            unknown_fields = set(necessary_fields) - RESTRICTED_PARTICIPANT_FIELDS
            if unknown_fields:
                raise ValidationError(f"necessary_fields 含有不支持的字段: {sorted(unknown_fields)}")
            if is_responsible and connection.execute(
                    "SELECT 1 FROM sched_posts WHERE zone_id=? AND is_responsible=1",
                    (zone_id,)).fetchone():
                raise ConflictError("该专区已存在责任人岗位")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO sched_posts(post_id,zone_id,name,required_skill,min_staff,"
                        "is_responsible,necessary_fields_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (post_id, zone_id, name, required_skill, min_staff, 1 if is_responsible else 0,
                         canonical_json(sorted(set(necessary_fields))), self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("岗位编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="post.registered",
                             resource_type="post", resource_id=post_id,
                             detail={"zone_id": zone_id, "name": name,
                                     "required_skill": required_skill, "min_staff": min_staff,
                                     "is_responsible": bool(is_responsible)},
                             occurred_at=self._now_text())
                return "post", post_id, {"post_id": post_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_post", payload=payload, create=create)

    def add_post_dependency(self, *, request_id: str, actor_id: str, post_id: str,
                            depends_on_post_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "post_id": post_id,
                   "depends_on_post_id": depends_on_post_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            post = self._post(connection, post_id)
            depends_on = self._post(connection, depends_on_post_id)
            self._manage_authority(actor, self._site(
                connection, self._zone(connection, post["zone_id"])["site_id"])["organization_id"])
            if post["zone_id"] != depends_on["zone_id"]:
                raise ValidationError("岗位依赖必须位于同一专区")
            if post_id == depends_on_post_id:
                raise ValidationError("岗位不能依赖自身")
            if self._dependency_reachable(connection, depends_on_post_id, post_id):
                raise ValidationError("岗位依赖不能形成环路")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO sched_post_dependencies(post_id,depends_on_post_id) VALUES(?,?)",
                        (post_id, depends_on_post_id),
                    )
                except Exception as exc:
                    raise ConflictError("岗位依赖已经存在") from exc
                append_event(connection, actor_id=actor_id, action="post_dependency.added",
                             resource_type="post", resource_id=post_id,
                             detail={"depends_on_post_id": depends_on_post_id},
                             occurred_at=self._now_text())
                return "post_dependency", f"{post_id}->{depends_on_post_id}", {
                    "post_id": post_id, "depends_on_post_id": depends_on_post_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="add_post_dependency", payload=payload, create=create)

    def register_shift(self, *, request_id: str, actor_id: str, shift_id: str, zone_id: str,
                       name: str, start_at: str, end_at: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "shift_id": shift_id, "zone_id": zone_id,
                   "name": name, "start_at": start_at, "end_at": end_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            zone = self._zone(connection, zone_id)
            self._manage_authority(actor, self._site(connection, zone["site_id"])["organization_id"])
            shift_id = self._identifier(shift_id, "shift_id")
            name = self._text(name, "name")
            start = self._parse_ts(start_at, "start_at")
            end = self._parse_ts(end_at, "end_at")
            if not start < end:
                raise ValidationError("start_at 必须早于 end_at")
            start_text, end_text = self._fmt(start), self._fmt(end)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO sched_shifts(shift_id,zone_id,name,start_at,end_at,created_at) "
                        "VALUES(?,?,?,?,?,?)",
                        (shift_id, zone_id, name, start_text, end_text, self._now_text()),
                    )
                except Exception as exc:
                    raise ConflictError("班次编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="shift.registered",
                             resource_type="shift", resource_id=shift_id,
                             detail={"zone_id": zone_id, "name": name,
                                     "start_at": start_text, "end_at": end_text},
                             occurred_at=self._now_text())
                return "shift", shift_id, {"shift_id": shift_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_shift", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 排班确认与版本
    # ------------------------------------------------------------------

    def confirm_schedule(self, *, request_id: str, actor_id: str, shift_id: str,
                         assignments: list[dict[str, str]]) -> WriteReceipt:
        """确认一份完整排班方案，生成新的已确认版本；校验失败不留半套变更。"""

        plan = self._normalize_plan(assignments)
        payload = {"actor_id": actor_id, "shift_id": shift_id, "assignments": plan}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            shift = self._shift(connection, shift_id)
            site_id = self._zone(connection, shift["zone_id"])["site_id"]
            self._dispatch_authority(connection, actor, site_id)
            replay = self._receipt_replay(connection, request_id=request_id,
                                          action="confirm_schedule", payload=payload)
            if replay is not None:
                return replay
            issues = self._validate_plan(connection, shift, plan)
            if issues:
                raise PlanRejected(issues)
            version_id, version_no = self._next_version(
                connection, shift_id, actor_id, source="manual")
            self._insert_assignments(connection, version_id, shift_id, plan)
            append_event(connection, actor_id=actor_id, action="schedule.confirmed",
                         resource_type="shift", resource_id=shift_id,
                         detail={"version_id": version_id, "version_no": version_no,
                                 "assignments": len(plan)},
                         occurred_at=self._now_text())
            response = {"version_id": version_id, "version_no": version_no}
            self._receipt_store(connection, request_id=request_id, action="confirm_schedule",
                                payload=payload, resource_type="schedule_version",
                                resource_id=version_id, response=response)
            return WriteReceipt(self._identifier(request_id, "request_id"),
                                "schedule_version", version_id, False)

    def _normalize_plan(self, assignments: Any) -> list[dict[str, str]]:
        if not isinstance(assignments, list) or not assignments:
            raise ValidationError("assignments 必须是非空数组")
        plan = []
        for index, entry in enumerate(assignments):
            if not isinstance(entry, dict):
                raise ValidationError(f"assignments[{index}] 必须是对象")
            post_id = str(entry.get("post_id", "")).strip()
            participant_id = str(entry.get("participant_id", "")).strip()
            if not post_id or not participant_id:
                raise ValidationError(f"assignments[{index}] 缺少 post_id 或 participant_id")
            plan.append({"post_id": post_id, "participant_id": participant_id})
        return plan

    def _next_version(self, connection, shift_id: str, actor_id: str,
                      source: str) -> tuple[str, int]:
        row = connection.execute(
            "SELECT MAX(version_no) AS max_no FROM sched_versions WHERE shift_id=?",
            (shift_id,)).fetchone()
        version_no = (row["max_no"] or 0) + 1
        connection.execute(
            "UPDATE sched_versions SET status='superseded' WHERE shift_id=? AND status='confirmed'",
            (shift_id,))
        version_id = uuid.uuid4().hex
        connection.execute(
            "INSERT INTO sched_versions(version_id,shift_id,version_no,status,source,confirmed_by,"
            "confirmed_at) VALUES(?,?,?,'confirmed',?,?,?)",
            (version_id, shift_id, version_no, source, actor_id, self._now_text()),
        )
        return version_id, version_no

    def _insert_assignments(self, connection, version_id: str, shift_id: str,
                            plan: list[dict[str, str]]) -> None:
        seen = set()
        for entry in plan:
            key = (entry["post_id"], entry["participant_id"])
            if key in seen:
                continue
            seen.add(key)
            connection.execute(
                "INSERT INTO sched_assignments(assignment_id,version_id,shift_id,post_id,"
                "participant_id) VALUES(?,?,?,?,?)",
                (uuid.uuid4().hex, version_id, shift_id, entry["post_id"], entry["participant_id"]),
            )

    def _validate_plan(self, connection, shift, plan: list[dict[str, str]]) -> list[PlanIssue]:
        """校验完整方案：资质、可服务时间、时间冲突、最低在岗数与岗位依赖。"""

        posts = {row["post_id"]: row for row in connection.execute(
            "SELECT * FROM sched_posts WHERE zone_id=?", (shift["zone_id"],))}
        issues: list[PlanIssue] = []
        counts: dict[str, int] = {}
        seen: set[str] = set()
        for entry in plan:
            post_id = entry["post_id"]
            participant_id = entry["participant_id"]
            post = posts.get(post_id)
            if post is None:
                raise ValidationError(f"岗位 {post_id} 不属于班次所在专区")
            counts[post_id] = counts.get(post_id, 0) + 1
            if participant_id in seen:
                issues.append(PlanIssue(
                    REASON_DUPLICATE_IN_PLAN,
                    f"参与者 {participant_id} 在同一班次被重复排班",
                    post_id=post_id, participant_id=participant_id))
                continue
            seen.add(participant_id)
            issues.extend(self._evaluate_candidate(connection, shift, post, participant_id))
        issues.extend(self._staffing_issues(posts.values(), counts))
        issues.extend(self._dependency_issues(connection, posts, counts))
        return issues

    def _evaluate_candidate(self, connection, shift, post,
                            participant_id: str) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        row = connection.execute(
            "SELECT * FROM sched_participants WHERE participant_id=?",
            (participant_id,)).fetchone()
        if row is None:
            raise ValidationError(f"参与者 {participant_id} 不存在")
        if not row["active"]:
            issues.append(PlanIssue(REASON_PARTICIPANT_INACTIVE,
                                    f"参与者 {participant_id} 已停用",
                                    post_id=post["post_id"], participant_id=participant_id))
        qualifications = connection.execute(
            "SELECT * FROM sched_qualifications WHERE participant_id=? AND skill=?",
            (participant_id, post["required_skill"])).fetchall()
        if not qualifications:
            issues.append(PlanIssue(
                REASON_QUALIFICATION_MISSING,
                f"参与者 {participant_id} 缺少岗位「{post['name']}」所需资质 {post['required_skill']}",
                post_id=post["post_id"], participant_id=participant_id))
        elif not any(q["valid_from"] <= shift["start_at"] and q["valid_until"] >= shift["end_at"]
                     for q in qualifications):
            issues.append(PlanIssue(
                REASON_QUALIFICATION_EXPIRED,
                f"参与者 {participant_id} 的资质有效期不能覆盖班次时段",
                post_id=post["post_id"], participant_id=participant_id))
        available = connection.execute(
            "SELECT 1 FROM sched_availability WHERE participant_id=? AND start_at<=? AND end_at>=? "
            "LIMIT 1",
            (participant_id, shift["start_at"], shift["end_at"])).fetchone()
        if available is None:
            issues.append(PlanIssue(
                REASON_AVAILABILITY_INSUFFICIENT,
                f"参与者 {participant_id} 的可服务时间不覆盖班次时段",
                post_id=post["post_id"], participant_id=participant_id))
        conflicts = connection.execute(
            "SELECT s.shift_id, s.start_at, s.end_at FROM sched_assignments a "
            "JOIN sched_versions v ON v.version_id=a.version_id AND v.status='confirmed' "
            "JOIN sched_shifts s ON s.shift_id=a.shift_id "
            "WHERE a.participant_id=? AND a.shift_id<>?",
            (participant_id, shift["shift_id"])).fetchall()
        for conflict in conflicts:
            if conflict["start_at"] < shift["end_at"] and conflict["end_at"] > shift["start_at"]:
                issues.append(PlanIssue(
                    REASON_TIME_CONFLICT,
                    f"参与者 {participant_id} 与已确认班次 {conflict['shift_id']} 时间冲突",
                    post_id=post["post_id"], participant_id=participant_id))
                break
        return issues

    def _staffing_issues(self, posts, counts: dict[str, int]) -> list[PlanIssue]:
        issues = []
        for post in posts:
            actual = counts.get(post["post_id"], 0)
            if actual < post["min_staff"]:
                issues.append(PlanIssue(
                    REASON_MIN_STAFF_SHORTAGE,
                    f"岗位「{post['name']}」在岗 {actual} 人，低于最低在岗数 {post['min_staff']}",
                    post_id=post["post_id"]))
        return issues

    def _dependency_issues(self, connection, posts: dict[str, Any],
                           counts: dict[str, int]) -> list[PlanIssue]:
        issues = []
        rows = connection.execute("SELECT * FROM sched_post_dependencies").fetchall()
        for row in rows:
            post = posts.get(row["post_id"])
            depends_on = posts.get(row["depends_on_post_id"])
            if post is None or depends_on is None:
                continue
            if counts.get(post["post_id"], 0) <= 0:
                continue
            required = max(1, depends_on["min_staff"])
            actual = counts.get(depends_on["post_id"], 0)
            if actual < required:
                issues.append(PlanIssue(
                    REASON_DEPENDENCY_UNCOVERED,
                    f"岗位「{post['name']}」依赖「{depends_on['name']}」，"
                    f"后者在岗 {actual} 人，低于所需 {required} 人",
                    post_id=post["post_id"]))
        return issues

    # ------------------------------------------------------------------
    # 缺员上报与替岗方案
    # ------------------------------------------------------------------

    def report_shortage(self, *, request_id: str, actor_id: str, shift_id: str,
                        participant_id: str, kind: str, expected_at: str | None = None,
                        note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "shift_id": shift_id, "participant_id": participant_id,
                   "kind": kind, "expected_at": expected_at, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            shift = self._shift(connection, shift_id)
            site_id = self._zone(connection, shift["zone_id"])["site_id"]
            self._dispatch_authority(connection, actor, site_id)
            replay = self._receipt_replay(connection, request_id=request_id,
                                          action="report_shortage", payload=payload)
            if replay is not None:
                return replay
            if kind not in SHORTAGE_KINDS:
                raise ValidationError("kind 不在允许范围内")
            note = self._optional_text(note, "note", 400)
            expected_text = None
            if kind == "late":
                if not expected_at:
                    raise ValidationError("迟到缺员必须提供 expected_at 预计到达时间")
                expected_text = self._ts_text(expected_at, "expected_at")
                if expected_text <= shift["start_at"]:
                    raise ValidationError("expected_at 必须晚于班次开始时间")
            elif expected_at:
                expected_text = self._ts_text(expected_at, "expected_at")
            version = self._current_version(connection, shift_id)
            if version is None:
                raise ValidationError("班次尚无已确认版本，不能上报缺员")
            assigned = connection.execute(
                "SELECT 1 FROM sched_assignments WHERE version_id=? AND participant_id=? LIMIT 1",
                (version["version_id"], participant_id)).fetchone()
            if assigned is None:
                raise ValidationError("该参与者不在当前班次的已确认排班中")

            def find_existing():
                row = connection.execute(
                    "SELECT shortage_id FROM sched_shortages WHERE shift_id=? AND participant_id=? "
                    "AND kind=? AND status='open' ORDER BY created_at LIMIT 1",
                    (shift_id, participant_id, kind)).fetchone()
                if row is None:
                    return None
                return "shortage", row["shortage_id"], {"shortage_id": row["shortage_id"]}

            def create() -> tuple[str, str, dict[str, Any]]:
                shortage_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO sched_shortages(shortage_id,shift_id,participant_id,kind,expected_at,"
                    "note,status,created_by,created_at) VALUES(?,?,?,?,?,?,'open',?,?)",
                    (shortage_id, shift_id, participant_id, kind, expected_text, note,
                     actor_id, self._now_text()),
                )
                append_event(connection, actor_id=actor_id, action="shortage.reported",
                             resource_type="shortage", resource_id=shortage_id,
                             detail={"shift_id": shift_id, "participant_id": participant_id,
                                     "kind": kind, "expected_at": expected_text},
                             occurred_at=self._now_text())
                return "shortage", shortage_id, {"shortage_id": shortage_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="report_shortage", payload=payload, create=create,
                                    find_existing=find_existing)

    def generate_proposals(self, *, actor_id: str, shortage_id: str,
                           regenerate: bool = False, limit: int = 3) -> dict[str, Any]:
        """为缺员生成多种完整替岗方案，并记录每名候选人被拒绝的具体原因。"""

        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            shortage = self._shortage(connection, shortage_id)
            shift = self._shift(connection, shortage["shift_id"])
            site_id = self._zone(connection, shift["zone_id"])["site_id"]
            self._dispatch_authority(connection, actor, site_id)
            if shortage["status"] != "open":
                raise ConflictError("缺员事件已处理完毕")
            existing = connection.execute(
                "SELECT proposal_id FROM sched_proposals WHERE shortage_id=? AND status='pending'",
                (shortage_id,)).fetchall()
            if existing and not regenerate:
                return {"shortage_id": shortage_id,
                        "proposals": self._proposal_views(connection, shortage_id),
                        "rejections": self._rejection_rows(connection, shortage_id)}
            if existing:
                connection.execute(
                    "UPDATE sched_proposals SET status='superseded' WHERE shortage_id=? "
                    "AND status='pending'", (shortage_id,))
                connection.execute(
                    "DELETE FROM sched_dispatch_rejections WHERE shortage_id=?", (shortage_id,))
            version = self._current_version(connection, shortage["shift_id"])
            if version is None:
                raise ConflictError("班次尚无已确认版本")
            affected = connection.execute(
                "SELECT a.post_id, p.name AS post_name FROM sched_assignments a "
                "JOIN sched_posts p ON p.post_id=a.post_id "
                "WHERE a.version_id=? AND a.participant_id=? ORDER BY a.post_id",
                (version["version_id"], shortage["participant_id"])).fetchall()
            if not affected:
                raise ConflictError("该参与者在当前版本中没有指派，无法生成替岗方案")
            current_plan = [dict(row) for row in connection.execute(
                "SELECT post_id, participant_id FROM sched_assignments WHERE version_id=?",
                (version["version_id"],))]
            assigned_elsewhere = {entry["participant_id"] for entry in current_plan}
            candidates = connection.execute(
                "SELECT * FROM sched_participants WHERE site_id=? AND participant_id<>? "
                "ORDER BY created_at, participant_id",
                (site_id, shortage["participant_id"])).fetchall()

            eligible_by_post: dict[str, list[str]] = {}
            for post_row in affected:
                post = self._post(connection, post_row["post_id"])
                eligible: list[str] = []
                for candidate in candidates:
                    reasons = self._candidate_rejection_reasons(
                        connection, shift, post, candidate, assigned_elsewhere)
                    if reasons:
                        connection.execute(
                            "INSERT INTO sched_dispatch_rejections(shortage_id,participant_id,"
                            "post_id,reasons_json,created_at) VALUES(?,?,?,?,?)",
                            (shortage_id, candidate["participant_id"], post["post_id"],
                             canonical_json(reasons), self._now_text()))
                    else:
                        eligible.append(candidate["participant_id"])
                eligible_by_post[post["post_id"]] = eligible[:limit]

            proposals: list[dict[str, Any]] = []
            combo_count = max((len(ids) for ids in eligible_by_post.values()), default=0)
            for index in range(min(combo_count, limit)):
                changes: list[dict[str, Any]] = []
                complete = True
                for post_row in affected:
                    pool = eligible_by_post[post_row["post_id"]]
                    if index >= len(pool):
                        complete = False
                        break
                    if shortage["kind"] != "late":
                        changes.append({"action": "remove", "post_id": post_row["post_id"],
                                        "participant_id": shortage["participant_id"]})
                    changes.append({"action": "add", "post_id": post_row["post_id"],
                                    "participant_id": pool[index]})
                if not complete:
                    continue
                impact = self._proposal_impact(connection, shift, current_plan, changes,
                                               shortage, affected)
                proposal_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO sched_proposals(proposal_id,shortage_id,shift_id,changes_json,"
                    "impact_json,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
                    (proposal_id, shortage_id, shift["shift_id"], canonical_json(changes),
                     canonical_json(impact), self._now_text()),
                )
                proposals.append({"proposal_id": proposal_id, "shortage_id": shortage_id,
                                  "shift_id": shift["shift_id"], "status": "pending",
                                  "changes": changes, "impact": impact})
            append_event(connection, actor_id=actor_id, action="proposals.generated",
                         resource_type="shortage", resource_id=shortage_id,
                         detail={"shift_id": shift["shift_id"], "proposals": len(proposals),
                                 "regenerate": bool(regenerate)},
                         occurred_at=self._now_text())
            return {"shortage_id": shortage_id, "proposals": proposals,
                    "rejections": self._rejection_rows(connection, shortage_id)}

    def _candidate_rejection_reasons(self, connection, shift, post, candidate,
                                     assigned_elsewhere: set[str]) -> list[str]:
        participant_id = candidate["participant_id"]
        issues = self._evaluate_candidate(connection, shift, post, participant_id)
        reasons = [issue.reason for issue in issues]
        if participant_id in assigned_elsewhere:
            reasons.append(REASON_DUPLICATE_IN_PLAN)
        return reasons

    def _proposal_impact(self, connection, shift, current_plan, changes, shortage,
                         affected) -> dict[str, Any]:
        after_plan = [dict(entry) for entry in current_plan]
        for change in changes:
            if change["action"] == "remove":
                after_plan = [entry for entry in after_plan
                              if not (entry["post_id"] == change["post_id"]
                                      and entry["participant_id"] == change["participant_id"])]
            else:
                after_plan.append({"post_id": change["post_id"],
                                   "participant_id": change["participant_id"]})
        posts = {row["post_id"]: row for row in connection.execute(
            "SELECT * FROM sched_posts WHERE zone_id=?", (shift["zone_id"],))}
        before_counts = self._plan_counts(current_plan)
        after_counts = self._plan_counts(after_plan)
        affected_post_ids = sorted({row["post_id"] for row in affected})
        staffing = [{
            "post_id": post_id,
            "post_name": posts[post_id]["name"],
            "before": before_counts.get(post_id, 0),
            "after": after_counts.get(post_id, 0),
            "min_staff": posts[post_id]["min_staff"],
        } for post_id in affected_post_ids]
        dependencies = []
        for row in connection.execute("SELECT * FROM sched_post_dependencies"):
            post = posts.get(row["post_id"])
            depends_on = posts.get(row["depends_on_post_id"])
            if post is None or depends_on is None:
                continue
            required = max(1, depends_on["min_staff"])
            dependencies.append({
                "post_id": post["post_id"],
                "post_name": post["name"],
                "depends_on_id": depends_on["post_id"],
                "depends_on_name": depends_on["name"],
                "covered_before": before_counts.get(depends_on["post_id"], 0) >= required,
                "covered_after": after_counts.get(depends_on["post_id"], 0) >= required,
            })
        names = {row["participant_id"]: row["name"] for row in connection.execute(
            "SELECT participant_id, name FROM sched_participants")}
        notes = []
        for change in changes:
            post_name = posts[change["post_id"]]["name"]
            participant_name = names.get(change["participant_id"], change["participant_id"])
            if change["action"] == "add":
                notes.append(f"{participant_name} 加入岗位「{post_name}」")
            else:
                notes.append(f"{participant_name} 退出岗位「{post_name}」")
        if shortage["kind"] == "late":
            notes.append(f"原排班保留，{names.get(shortage['participant_id'], shortage['participant_id'])}"
                         f" 预计 {shortage['expected_at']} 到岗，替岗人员覆盖到岗前的空缺")
        return {"staffing": staffing, "dependencies": dependencies, "notes": notes}

    @staticmethod
    def _plan_counts(plan) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in plan:
            counts[entry["post_id"]] = counts.get(entry["post_id"], 0) + 1
        return counts

    def confirm_proposal(self, *, request_id: str, actor_id: str,
                         proposal_id: str) -> WriteReceipt:
        """负责人确认某个完整替岗方案后才更新排班；校验失败整体回滚。"""

        payload = {"actor_id": actor_id, "proposal_id": proposal_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute(
                "SELECT * FROM sched_proposals WHERE proposal_id=?",
                (proposal_id,)).fetchone()
            if row is None:
                raise NotFoundError("替岗方案不存在")
            if row["status"] != "pending":
                raise ConflictError("替岗方案不在待确认状态")
            shortage = self._shortage(connection, row["shortage_id"])
            if shortage["status"] != "open":
                raise ConflictError("缺员事件已处理完毕")
            shift = self._shift(connection, row["shift_id"])
            site_id = self._zone(connection, shift["zone_id"])["site_id"]
            self._dispatch_authority(connection, actor, site_id)
            replay = self._receipt_replay(connection, request_id=request_id,
                                          action="confirm_proposal", payload=payload)
            if replay is not None:
                return replay
            changes = json.loads(row["changes_json"])
            version = self._current_version(connection, shift["shift_id"])
            if version is None:
                raise ConflictError("班次尚无已确认版本")
            current_plan = [dict(entry) for entry in connection.execute(
                "SELECT post_id, participant_id FROM sched_assignments WHERE version_id=?",
                (version["version_id"],))]
            current_pairs = {(entry["post_id"], entry["participant_id"]) for entry in current_plan}
            for change in changes:
                pair = (change["post_id"], change["participant_id"])
                if change["action"] == "remove" and pair not in current_pairs:
                    raise ConflictError("替岗方案已过期，请重新生成")
                if change["action"] == "add" and pair in current_pairs:
                    raise ConflictError("替岗方案已过期，请重新生成")
            new_plan = [dict(entry) for entry in current_plan]
            for change in changes:
                if change["action"] == "remove":
                    new_plan = [entry for entry in new_plan
                                if not (entry["post_id"] == change["post_id"]
                                        and entry["participant_id"] == change["participant_id"])]
                else:
                    new_plan.append({"post_id": change["post_id"],
                                     "participant_id": change["participant_id"]})
            issues = self._validate_plan(connection, shift, new_plan)
            if issues:
                raise PlanRejected(issues)
            version_id, version_no = self._next_version(
                connection, shift["shift_id"], actor_id, source=f"proposal:{proposal_id}")
            self._insert_assignments(connection, version_id, shift["shift_id"], new_plan)
            connection.execute(
                "UPDATE sched_proposals SET status='confirmed' WHERE proposal_id=?",
                (proposal_id,))
            connection.execute(
                "UPDATE sched_proposals SET status='superseded' WHERE shortage_id=? "
                "AND status='pending'", (shortage["shortage_id"],))
            connection.execute(
                "UPDATE sched_shortages SET status='resolved' WHERE shortage_id=?",
                (shortage["shortage_id"],))
            append_event(connection, actor_id=actor_id, action="proposal.confirmed",
                         resource_type="proposal", resource_id=proposal_id,
                         detail={"shift_id": shift["shift_id"], "shortage_id": shortage["shortage_id"],
                                 "version_id": version_id, "version_no": version_no},
                         occurred_at=self._now_text())
            response = {"version_id": version_id, "version_no": version_no}
            self._receipt_store(connection, request_id=request_id, action="confirm_proposal",
                                payload=payload, resource_type="schedule_version",
                                resource_id=version_id, response=response)
            return WriteReceipt(self._identifier(request_id, "request_id"),
                                "schedule_version", version_id, False)

    # ------------------------------------------------------------------
    # 签到事实（不可回写，重复签到与迟到回执幂等）
    # ------------------------------------------------------------------

    def record_checkin(self, *, request_id: str, actor_id: str, shift_id: str,
                       participant_id: str, kind: str = "checkin",
                       occurred_at: str | None = None) -> WriteReceipt:
        payload = {"actor_id": actor_id, "shift_id": shift_id, "participant_id": participant_id,
                   "kind": kind, "occurred_at": occurred_at}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            shift = self._shift(connection, shift_id)
            site_id = self._zone(connection, shift["zone_id"])["site_id"]
            self._dispatch_authority(connection, actor, site_id)
            replay = self._receipt_replay(connection, request_id=request_id,
                                          action="record_checkin", payload=payload)
            if replay is not None:
                return replay
            if kind not in CHECKIN_KINDS:
                raise ValidationError("kind 不在允许范围内")
            occurred_text = (self._ts_text(occurred_at, "occurred_at")
                             if occurred_at else self._now_text())
            version = self._current_version(connection, shift_id)
            if version is None:
                raise ValidationError("班次尚无已确认版本，不能签到")
            assigned = connection.execute(
                "SELECT 1 FROM sched_assignments WHERE version_id=? AND participant_id=? LIMIT 1",
                (version["version_id"], participant_id)).fetchone()
            if assigned is None:
                raise ValidationError("该参与者不在当前班次的已确认排班中")

            def find_existing():
                row = connection.execute(
                    "SELECT checkin_id FROM sched_checkins WHERE shift_id=? AND participant_id=? "
                    "AND kind=?",
                    (shift_id, participant_id, kind)).fetchone()
                if row is None:
                    return None
                return "checkin", row["checkin_id"], {"checkin_id": row["checkin_id"]}

            def create() -> tuple[str, str, dict[str, Any]]:
                checkin_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO sched_checkins(checkin_id,shift_id,participant_id,kind,occurred_at,"
                    "recorded_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (checkin_id, shift_id, participant_id, kind, occurred_text,
                     actor_id, self._now_text()),
                )
                append_event(connection, actor_id=actor_id, action="checkin.recorded",
                             resource_type="checkin", resource_id=checkin_id,
                             detail={"shift_id": shift_id, "participant_id": participant_id,
                                     "kind": kind, "occurred_at": occurred_text},
                             occurred_at=self._now_text())
                return "checkin", checkin_id, {"checkin_id": checkin_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_checkin", payload=payload, create=create,
                                    find_existing=find_existing)

    # ------------------------------------------------------------------
    # 紧急接管与交接
    # ------------------------------------------------------------------

    def register_takeover(self, *, request_id: str, actor_id: str, site_id: str,
                          holder_id: str, reason: str, valid_from: str, valid_until: str,
                          handover_items: list[str]) -> WriteReceipt:
        items = handover_items or []
        payload = {"actor_id": actor_id, "site_id": site_id, "holder_id": holder_id,
                   "reason": reason, "valid_from": valid_from, "valid_until": valid_until,
                   "handover_items": items}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            site = self._site(connection, site_id)
            self._manage_authority(actor, site["organization_id"])
            replay = self._receipt_replay(connection, request_id=request_id,
                                          action="register_takeover", payload=payload)
            if replay is not None:
                return replay
            holder = self._actor(connection, holder_id)
            if holder.organization_id != site["organization_id"]:
                raise ValidationError("接管人必须属于场所所在机构")
            reason = self._text(reason, "reason", 400)
            start = self._parse_ts(valid_from, "valid_from")
            end = self._parse_ts(valid_until, "valid_until")
            if not start < end:
                raise ValidationError("valid_from 必须早于 valid_until")
            if end <= self._now():
                raise ValidationError("接管有效期限已经届满")
            if not isinstance(items, list) or not items:
                raise ValidationError("handover_items 必须是非空数组")
            items = [self._text(item, "handover_items[]", 200) for item in items]
            start_text, end_text = self._fmt(start), self._fmt(end)

            def create() -> tuple[str, str, dict[str, Any]]:
                takeover_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO sched_takeovers(takeover_id,site_id,holder_id,reason,valid_from,"
                    "valid_until,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (takeover_id, site_id, holder_id, reason, start_text, end_text,
                     actor_id, self._now_text()),
                )
                for item in items:
                    connection.execute(
                        "INSERT INTO sched_handovers(handover_id,takeover_id,item,status) "
                        "VALUES(?,?,?,'pending')",
                        (uuid.uuid4().hex, takeover_id, item),
                    )
                append_event(connection, actor_id=actor_id, action="takeover.registered",
                             resource_type="takeover", resource_id=takeover_id,
                             detail={"site_id": site_id, "holder_id": holder_id,
                                     "reason": reason, "valid_from": start_text,
                                     "valid_until": end_text, "handover_items": len(items)},
                             occurred_at=self._now_text())
                return "takeover", takeover_id, {"takeover_id": takeover_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_takeover", payload=payload, create=create)

    def complete_handover(self, *, request_id: str, actor_id: str,
                          handover_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "handover_id": handover_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            row = connection.execute(
                "SELECT h.*, t.holder_id, t.site_id FROM sched_handovers h "
                "JOIN sched_takeovers t ON t.takeover_id=h.takeover_id WHERE h.handover_id=?",
                (handover_id,)).fetchone()
            if row is None:
                raise NotFoundError("交接事项不存在")
            site = self._site(connection, row["site_id"])
            if actor.actor_id != row["holder_id"]:
                self._manage_authority(actor, site["organization_id"])

            def find_existing():
                if row["status"] != "done":
                    return None
                return "handover", handover_id, {"handover_id": handover_id}

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "UPDATE sched_handovers SET status='done', completed_by=?, completed_at=? "
                    "WHERE handover_id=?",
                    (actor_id, self._now_text(), handover_id),
                )
                append_event(connection, actor_id=actor_id, action="handover.completed",
                             resource_type="handover", resource_id=handover_id,
                             detail={"takeover_id": row["takeover_id"], "item": row["item"]},
                             occurred_at=self._now_text())
                return "handover", handover_id, {"handover_id": handover_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="complete_handover", payload=payload, create=create,
                                    find_existing=find_existing)

    def pending_handovers(self, *, actor_id: str, site_id: str) -> list[dict[str, Any]]:
        """列出尚未完成的交接事项；服务重启后据此接续。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            site = self._site(connection, site_id)
            self._read_authority(actor, site["organization_id"])
            rows = connection.execute(
                "SELECT h.handover_id, h.item, h.status, h.takeover_id, t.holder_id, t.reason, "
                "t.valid_from, t.valid_until FROM sched_handovers h "
                "JOIN sched_takeovers t ON t.takeover_id=h.takeover_id "
                "WHERE t.site_id=? AND h.status='pending' ORDER BY t.valid_until, h.handover_id",
                (site_id,)).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 台账查询
    # ------------------------------------------------------------------

    def zone_responsible(self, *, actor_id: str, zone_id: str, at: str) -> dict[str, Any]:
        """回答指定时刻的专区责任人、调度权归属与未覆盖依赖。"""

        at_text = self._ts_text(at, "at")
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            zone = self._zone(connection, zone_id)
            self._read_authority(actor, self._site(connection, zone["site_id"])["organization_id"])
            shift = connection.execute(
                "SELECT * FROM sched_shifts WHERE zone_id=? AND start_at<=? AND end_at>? "
                "ORDER BY start_at DESC LIMIT 1",
                (zone_id, at_text, at_text)).fetchone()
            takeover = connection.execute(
                "SELECT * FROM sched_takeovers WHERE site_id=? AND valid_from<=? AND valid_until>? "
                "ORDER BY valid_until DESC LIMIT 1",
                (zone["site_id"], at_text, at_text)).fetchone()
            dispatch = None
            if takeover:
                dispatch = {"takeover_id": takeover["takeover_id"],
                            "holder_id": takeover["holder_id"],
                            "reason": takeover["reason"],
                            "valid_until": takeover["valid_until"]}
            responsible = None
            uncovered: list[dict[str, Any]] = []
            shift_id = None
            if shift is not None:
                shift_id = shift["shift_id"]
                version = self._current_version(connection, shift_id)
                post = connection.execute(
                    "SELECT * FROM sched_posts WHERE zone_id=? AND is_responsible=1 LIMIT 1",
                    (zone_id,)).fetchone()
                if version is not None and post is not None:
                    assignment = connection.execute(
                        "SELECT participant_id FROM sched_assignments WHERE version_id=? "
                        "AND post_id=? ORDER BY participant_id LIMIT 1",
                        (version["version_id"], post["post_id"])).fetchone()
                    if assignment:
                        participant = self._participant(connection, assignment["participant_id"])
                        responsible = {"participant_id": participant["participant_id"],
                                       "name": participant["name"],
                                       "post_id": post["post_id"], "post_name": post["name"],
                                       "version_no": version["version_no"]}
                uncovered = self._uncovered(connection, shift, at_text)["issues"]
            return {"zone_id": zone_id, "at": at_text, "shift_id": shift_id,
                    "responsible": responsible, "dispatch_authority": dispatch,
                    "uncovered_dependencies": uncovered}

    def uncovered_dependencies(self, *, actor_id: str, shift_id: str,
                               at: str | None = None) -> dict[str, Any]:
        """回答班次在指定时刻（默认当前）的未覆盖依赖与在岗缺口。"""

        at_text = self._ts_text(at, "at") if at else self._now_text()
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            shift = self._shift(connection, shift_id)
            site_id = self._zone(connection, shift["zone_id"])["site_id"]
            self._read_authority(actor, self._site(connection, site_id)["organization_id"])
            result = self._uncovered(connection, shift, at_text)
            return {"shift_id": shift_id, "at": at_text, **result}

    def _uncovered(self, connection, shift, at_text: str) -> dict[str, Any]:
        posts = {row["post_id"]: row for row in connection.execute(
            "SELECT * FROM sched_posts WHERE zone_id=?", (shift["zone_id"],))}
        version = self._current_version(connection, shift["shift_id"])
        counts: dict[str, int] = {}
        version_no = None
        if version is not None:
            version_no = version["version_no"]
            effective = self._effective_assignments(connection, shift, version, at_text)
            counts = self._plan_counts(effective)
        staffing_gaps = [
            {"post_id": post["post_id"], "post_name": post["name"],
             "required": post["min_staff"], "actual": counts.get(post["post_id"], 0)}
            for post in posts.values() if counts.get(post["post_id"], 0) < post["min_staff"]]
        issues = [
            {"post_id": issue.post_id, "reason": issue.reason, "message": issue.message}
            for issue in self._dependency_issues(connection, posts, counts)]
        return {"version_no": version_no, "issues": issues, "staffing_gaps": staffing_gaps}

    def _effective_assignments(self, connection, shift, version, at_text: str):
        """已确认指派减去在指定时刻处于缺员状态的参与者。"""

        rows = connection.execute(
            "SELECT post_id, participant_id FROM sched_assignments WHERE version_id=?",
            (version["version_id"],)).fetchall()
        shortages = connection.execute(
            "SELECT participant_id, kind, expected_at FROM sched_shortages "
            "WHERE shift_id=? AND status='open'",
            (shift["shift_id"],)).fetchall()
        absent = set()
        for shortage in shortages:
            if shortage["kind"] in ("absent", "early_leave"):
                absent.add(shortage["participant_id"])
            elif (shortage["kind"] == "late" and shortage["expected_at"]
                  and at_text < shortage["expected_at"]):
                absent.add(shortage["participant_id"])
        return [dict(row) for row in rows if row["participant_id"] not in absent]

    def dispatch_rejections(self, *, actor_id: str, shortage_id: str) -> list[dict[str, Any]]:
        """回答替岗评估中每名候选人被拒绝调度的具体原因。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            shortage = self._shortage(connection, shortage_id)
            shift = self._shift(connection, shortage["shift_id"])
            site_id = self._zone(connection, shift["zone_id"])["site_id"]
            self._read_authority(actor, self._site(connection, site_id)["organization_id"])
            return self._rejection_rows(connection, shortage_id)

    def _rejection_rows(self, connection, shortage_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT participant_id, post_id, reasons_json FROM sched_dispatch_rejections "
            "WHERE shortage_id=? ORDER BY rejection_id",
            (shortage_id,)).fetchall()
        return [{"participant_id": row["participant_id"], "post_id": row["post_id"],
                 "reasons": json.loads(row["reasons_json"]),
                 "reason_text": [REASON_TEXT.get(reason, reason)
                                 for reason in json.loads(row["reasons_json"])]}
                for row in rows]

    def post_roster(self, *, actor_id: str, post_id: str, shift_id: str) -> dict[str, Any]:
        """按岗位履职必需字段返回当班人员，超出的信息一律不读出。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            post = self._post(connection, post_id)
            shift = self._shift(connection, shift_id)
            if shift["zone_id"] != post["zone_id"]:
                raise ValidationError("岗位不属于该班次所在专区")
            site_id = self._zone(connection, post["zone_id"])["site_id"]
            self._read_authority(actor, self._site(connection, site_id)["organization_id"])
            fields = set(PUBLIC_PARTICIPANT_FIELDS) | set(
                json.loads(post["necessary_fields_json"]))
            version = self._current_version(connection, shift_id)
            items = []
            if version is not None:
                rows = connection.execute(
                    "SELECT participant_id FROM sched_assignments WHERE version_id=? AND post_id=? "
                    "ORDER BY participant_id",
                    (version["version_id"], post_id)).fetchall()
                for row in rows:
                    participant = self._participant(connection, row["participant_id"])
                    items.append(self._participant_view(connection, participant, fields))
            return {"post_id": post_id, "shift_id": shift_id,
                    "version_no": version["version_no"] if version else None,
                    "visible_fields": sorted(fields), "items": items}

    def _participant_view(self, connection, participant, fields: set[str]) -> dict[str, Any]:
        view: dict[str, Any] = {}
        if "participant_id" in fields:
            view["participant_id"] = participant["participant_id"]
        if "name" in fields:
            view["name"] = participant["name"]
        if "role_type" in fields:
            view["role_type"] = participant["role_type"]
        if "phone" in fields:
            view["phone"] = participant["phone"]
        if "title" in fields:
            view["title"] = participant["title"]
        if "profile" in fields:
            view["profile"] = json.loads(participant["profile_json"])
        if "qualifications" in fields:
            rows = connection.execute(
                "SELECT skill, valid_from, valid_until FROM sched_qualifications "
                "WHERE participant_id=? ORDER BY skill",
                (participant["participant_id"],)).fetchall()
            view["qualifications"] = [dict(row) for row in rows]
        return view

    def shift_versions(self, *, actor_id: str, shift_id: str) -> list[dict[str, Any]]:
        """列出班次已确认版本的历史。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            shift = self._shift(connection, shift_id)
            site_id = self._zone(connection, shift["zone_id"])["site_id"]
            self._read_authority(actor, self._site(connection, site_id)["organization_id"])
            rows = connection.execute(
                "SELECT v.version_id, v.version_no, v.status, v.source, v.confirmed_by, "
                "v.confirmed_at, COUNT(a.assignment_id) AS assignments "
                "FROM sched_versions v LEFT JOIN sched_assignments a ON a.version_id=v.version_id "
                "WHERE v.shift_id=? GROUP BY v.version_id ORDER BY v.version_no",
                (shift_id,)).fetchall()
            return [dict(row) for row in rows]

    def get_participant(self, *, actor_id: str, participant_id: str) -> dict[str, Any]:
        """返回参与者完整台账资料，仅限具备管理权限的角色。"""

        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            participant = self._participant(connection, participant_id)
            site_id = participant["site_id"]
            self._manage_authority(actor, self._site(connection, site_id)["organization_id"])
            qualifications = connection.execute(
                "SELECT qualification_id, skill, valid_from, valid_until FROM sched_qualifications "
                "WHERE participant_id=? ORDER BY skill",
                (participant_id,)).fetchall()
            availability = connection.execute(
                "SELECT availability_id, start_at, end_at FROM sched_availability "
                "WHERE participant_id=? ORDER BY start_at",
                (participant_id,)).fetchall()
            return {"participant_id": participant["participant_id"], "site_id": site_id,
                    "name": participant["name"], "role_type": participant["role_type"],
                    "phone": participant["phone"], "title": participant["title"],
                    "profile": json.loads(participant["profile_json"]),
                    "active": bool(participant["active"]),
                    "qualifications": [dict(row) for row in qualifications],
                    "availability": [dict(row) for row in availability]}

    def list_proposals(self, *, actor_id: str, shortage_id: str) -> list[dict[str, Any]]:
        with self.database.transaction() as connection:
            actor = self._actor(connection, actor_id)
            shortage = self._shortage(connection, shortage_id)
            shift = self._shift(connection, shortage["shift_id"])
            site_id = self._zone(connection, shift["zone_id"])["site_id"]
            self._read_authority(actor, self._site(connection, site_id)["organization_id"])
            return self._proposal_views(connection, shortage_id)

    def _proposal_views(self, connection, shortage_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM sched_proposals WHERE shortage_id=? AND status='pending' "
            "ORDER BY rowid",
            (shortage_id,)).fetchall()
        return [{"proposal_id": row["proposal_id"], "shortage_id": row["shortage_id"],
                 "shift_id": row["shift_id"], "status": row["status"],
                 "changes": json.loads(row["changes_json"]),
                 "impact": json.loads(row["impact_json"])} for row in rows]

    # ------------------------------------------------------------------
    # 行装载工具
    # ------------------------------------------------------------------

    def _participant(self, connection, participant_id: str):
        row = connection.execute(
            "SELECT * FROM sched_participants WHERE participant_id=?",
            (participant_id,)).fetchone()
        if row is None:
            raise NotFoundError("参与者不存在")
        return row

    def _zone(self, connection, zone_id: str):
        row = connection.execute(
            "SELECT * FROM sched_zones WHERE zone_id=?", (zone_id,)).fetchone()
        if row is None:
            raise NotFoundError("专区不存在")
        return row

    def _post(self, connection, post_id: str):
        row = connection.execute(
            "SELECT * FROM sched_posts WHERE post_id=?", (post_id,)).fetchone()
        if row is None:
            raise NotFoundError("岗位不存在")
        return row

    def _shift(self, connection, shift_id: str):
        row = connection.execute(
            "SELECT * FROM sched_shifts WHERE shift_id=?", (shift_id,)).fetchone()
        if row is None:
            raise NotFoundError("班次不存在")
        return row

    def _shortage(self, connection, shortage_id: str):
        row = connection.execute(
            "SELECT * FROM sched_shortages WHERE shortage_id=?", (shortage_id,)).fetchone()
        if row is None:
            raise NotFoundError("缺员事件不存在")
        return row

    def _current_version(self, connection, shift_id: str):
        return connection.execute(
            "SELECT * FROM sched_versions WHERE shift_id=? AND status='confirmed' "
            "ORDER BY version_no DESC LIMIT 1",
            (shift_id,)).fetchone()

    def _dependency_reachable(self, connection, start_post_id: str,
                              target_post_id: str) -> bool:
        """沿既有依赖边判断 target 是否可达，用于阻止依赖环路。"""

        visited = set()
        stack = [start_post_id]
        while stack:
            current = stack.pop()
            if current == target_post_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            rows = connection.execute(
                "SELECT depends_on_post_id FROM sched_post_dependencies WHERE post_id=?",
                (current,)).fetchall()
            stack.extend(row["depends_on_post_id"] for row in rows)
        return False
