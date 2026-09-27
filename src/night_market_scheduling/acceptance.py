"""运行协同台账的离线端到端验收。

场景：第二届中医文化夜市义诊专区晚班。负责人确认完整排班后，
护理人员迟到，系统生成多种替岗方案并记录每名候选人被拒绝的具体原因；
负责人确认其中一个完整方案后排班才更新。随后演示签到幂等、紧急接管
到期自动失效、服务重启后接续未完成交接，以及哈希链审计校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from night_market_foundation.clock import FixedClock
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database

from .service import SchedulingService


def _services(path: Path, moment: datetime) -> tuple[Database, DomainService, SchedulingService]:
    database = Database(path)
    clock = FixedClock(moment)
    return database, DomainService(database, clock), SchedulingService(database, clock)


def run() -> dict[str, object]:
    """执行完整值班故事并返回验收结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "scheduling-acceptance.sqlite3"

        # ---- 筹备阶段（2026-09-30 08:00Z）：机构、台账资料、完整排班 ----
        database, foundation, scheduling = _services(
            path, datetime(2026, 9, 30, 8, 0, tzinfo=timezone.utc))
        foundation.register_organization(request_id="acc-org", actor_id="bootstrap",
                                         organization_id="org-001", name="中医文化夜市组委会")
        foundation.register_actor(request_id="acc-admin", actor_id="bootstrap",
                                  new_actor_id="admin-001", display_name="系统管理员",
                                  role="admin", organization_id="org-001")
        foundation.register_actor(request_id="acc-lead", actor_id="admin-001",
                                  new_actor_id="op-lead", display_name="排班负责人",
                                  role="operator", organization_id="org-001")
        foundation.register_actor(request_id="acc-duty", actor_id="admin-001",
                                  new_actor_id="rv-duty", display_name="值班调度员",
                                  role="reviewer", organization_id="org-001")
        foundation.register_site(request_id="acc-site", actor_id="op-lead", site_id="site-001",
                                 organization_id="org-001", name="夜市主会场",
                                 timezone_name="Asia/Shanghai")
        scheduling.register_zone(request_id="acc-zone", actor_id="op-lead",
                                 zone_id="zone-clinic", site_id="site-001", name="义诊专区")
        scheduling.register_post(request_id="acc-post-lead", actor_id="op-lead",
                                 post_id="post-lead", zone_id="zone-clinic", name="专区负责人",
                                 required_skill="coordination", min_staff=1,
                                 is_responsible=True, necessary_fields=["phone"])
        scheduling.register_post(request_id="acc-post-diag", actor_id="op-lead",
                                 post_id="post-diag", zone_id="zone-clinic", name="义诊诊疗",
                                 required_skill="diagnosis", min_staff=1,
                                 necessary_fields=["title"])
        scheduling.register_post(request_id="acc-post-accomp", actor_id="op-lead",
                                 post_id="post-accomp", zone_id="zone-clinic", name="义诊陪同",
                                 required_skill="accompaniment", min_staff=1,
                                 necessary_fields=["phone"])
        scheduling.register_post(request_id="acc-post-supply", actor_id="op-lead",
                                 post_id="post-supply", zone_id="zone-clinic", name="物资补给",
                                 required_skill="supply", min_staff=1,
                                 necessary_fields=["phone"])
        scheduling.register_post(request_id="acc-post-order", actor_id="op-lead",
                                 post_id="post-order", zone_id="zone-clinic", name="秩序维护",
                                 required_skill="order_keeping", min_staff=1)
        for index, depends_on in enumerate(("post-accomp", "post-supply", "post-order")):
            scheduling.add_post_dependency(request_id=f"acc-dep-{index}", actor_id="op-lead",
                                           post_id="post-diag", depends_on_post_id=depends_on)
        participants = [
            ("p-doctor", "林医师", "famous_doctor", "diagnosis", "主任医师"),
            ("p-nurse-a", "王护士", "nurse", "accompaniment", "主管护师"),
            ("p-nurse-b", "李护士", "nurse", "accompaniment", "护师"),
            ("p-log-a", "赵后勤", "logistics", "supply", ""),
            ("p-log-b", "钱后勤", "logistics", "order_keeping", ""),
            ("p-guide", "孙讲解", "volunteer_guide", "guiding", ""),
            ("p-lead", "周干事", "logistics", "coordination", ""),
        ]
        for participant_id, name, role_type, skill, title in participants:
            scheduling.register_participant(
                request_id=f"acc-part-{participant_id}", actor_id="op-lead",
                participant_id=participant_id, site_id="site-001", name=name,
                role_type=role_type, phone="13800000000", title=title)
            scheduling.add_qualification(
                request_id=f"acc-qual-{participant_id}", actor_id="op-lead",
                participant_id=participant_id, skill=skill,
                valid_from="2026-09-01T00:00:00Z", valid_until="2026-12-31T00:00:00Z")
            scheduling.add_availability(
                request_id=f"acc-avail-{participant_id}", actor_id="op-lead",
                participant_id=participant_id,
                start_at="2026-10-01T08:00:00Z", end_at="2026-10-01T16:00:00Z")
        scheduling.register_shift(request_id="acc-shift", actor_id="op-lead",
                                  shift_id="shift-evening", zone_id="zone-clinic",
                                  name="夜市晚班", start_at="2026-10-01T10:00:00Z",
                                  end_at="2026-10-01T14:00:00Z")
        scheduling.confirm_schedule(
            request_id="acc-confirm-1", actor_id="op-lead", shift_id="shift-evening",
            assignments=[
                {"post_id": "post-lead", "participant_id": "p-lead"},
                {"post_id": "post-diag", "participant_id": "p-doctor"},
                {"post_id": "post-accomp", "participant_id": "p-nurse-a"},
                {"post_id": "post-supply", "participant_id": "p-log-a"},
                {"post_id": "post-order", "participant_id": "p-log-b"},
            ])
        database.close()

        # ---- 班次开始（10:05Z）：签到幂等，随后王护士迟到上报 ----
        database, foundation, scheduling = _services(
            path, datetime(2026, 10, 1, 10, 5, tzinfo=timezone.utc))
        for participant_id in ("p-lead", "p-doctor", "p-nurse-a", "p-log-a", "p-log-b"):
            scheduling.record_checkin(request_id=f"acc-ci-{participant_id}", actor_id="op-lead",
                                      shift_id="shift-evening", participant_id=participant_id)
        checkin_replay = scheduling.record_checkin(
            request_id="acc-ci-p-lead", actor_id="op-lead",
            shift_id="shift-evening", participant_id="p-lead")
        checkin_natural_replay = scheduling.record_checkin(
            request_id="acc-ci-p-lead-again", actor_id="op-lead",
            shift_id="shift-evening", participant_id="p-lead")
        shortage = scheduling.report_shortage(
            request_id="acc-shortage", actor_id="op-lead", shift_id="shift-evening",
            participant_id="p-nurse-a", kind="late",
            expected_at="2026-10-01T11:30:00Z", note="王护士路上拥堵，预计迟到")
        shortage_id = shortage.resource_id
        uncovered_before = scheduling.uncovered_dependencies(
            actor_id="op-lead", shift_id="shift-evening", at="2026-10-01T10:30:00Z")
        proposals = scheduling.generate_proposals(actor_id="op-lead", shortage_id=shortage_id)
        responsible = scheduling.zone_responsible(
            actor_id="op-lead", zone_id="zone-clinic", at="2026-10-01T10:30:00Z")
        scheduling.confirm_proposal(request_id="acc-confirm-2", actor_id="op-lead",
                                    proposal_id=proposals["proposals"][0]["proposal_id"])
        uncovered_after = scheduling.uncovered_dependencies(
            actor_id="op-lead", shift_id="shift-evening", at="2026-10-01T10:30:00Z")
        versions = scheduling.shift_versions(actor_id="op-lead", shift_id="shift-evening")
        roster_accomp = scheduling.post_roster(actor_id="op-lead", post_id="post-accomp",
                                               shift_id="shift-evening")
        roster_order = scheduling.post_roster(actor_id="op-lead", post_id="post-order",
                                              shift_id="shift-evening")
        database.close()

        # ---- 王护士 11:35Z 到岗，迟到回执幂等 ----
        database, foundation, scheduling = _services(
            path, datetime(2026, 10, 1, 11, 35, tzinfo=timezone.utc))
        scheduling.record_checkin(request_id="acc-late-nurse-a", actor_id="op-lead",
                                  shift_id="shift-evening", participant_id="p-nurse-a",
                                  kind="late_receipt")
        late_replay = scheduling.record_checkin(
            request_id="acc-late-nurse-a", actor_id="op-lead",
            shift_id="shift-evening", participant_id="p-nurse-a", kind="late_receipt")
        database.close()

        # ---- 11:50Z 负责人临时离岗，登记紧急接管与交接事项 ----
        database, foundation, scheduling = _services(
            path, datetime(2026, 10, 1, 11, 50, tzinfo=timezone.utc))
        scheduling.register_takeover(
            request_id="acc-takeover", actor_id="op-lead", site_id="site-001",
            holder_id="rv-duty", reason="负责人临时离岗处理突发事务",
            valid_from="2026-10-01T12:00:00Z", valid_until="2026-10-01T13:00:00Z",
            handover_items=["对讲机与场地钥匙", "未完成的物资补给确认"])
        database.close()

        # ---- 12:35Z 服务重启：接续尚未完成的交接；接管期限内拥有调度权 ----
        database, foundation, scheduling = _services(
            path, datetime(2026, 10, 1, 12, 35, tzinfo=timezone.utc))
        responsible_in_window = scheduling.zone_responsible(
            actor_id="op-lead", zone_id="zone-clinic", at="2026-10-01T12:30:00Z")
        responsible_after_window = scheduling.zone_responsible(
            actor_id="op-lead", zone_id="zone-clinic", at="2026-10-01T13:30:00Z")
        pending = scheduling.pending_handovers(actor_id="op-lead", site_id="site-001")
        completed = 0
        for index, item in enumerate(pending):
            receipt = scheduling.complete_handover(
                request_id=f"acc-handover-{index}", actor_id="rv-duty",
                handover_id=item["handover_id"])
            completed += 0 if receipt.replayed else 1
        remaining = scheduling.pending_handovers(actor_id="op-lead", site_id="site-001")
        audit_valid, audit_events = foundation.verify_audit()
        result = {
            "status": "ok",
            "versions": len(versions),
            "proposals": len(proposals["proposals"]),
            "rejections": len(proposals["rejections"]),
            "uncovered_before": len(uncovered_before["issues"]),
            "uncovered_after": len(uncovered_after["issues"]),
            "responsible": responsible["responsible"]["participant_id"],
            "checkin_replayed": checkin_replay.replayed and checkin_natural_replay.replayed,
            "late_receipt_replayed": late_replay.replayed,
            "roster_phone_visible": "phone" in roster_accomp["items"][0],
            "roster_phone_hidden": "phone" not in roster_order["items"][0],
            "takeover_holder_in_window":
                responsible_in_window["dispatch_authority"]["holder_id"],
            "takeover_holder_after_window":
                responsible_after_window["dispatch_authority"],
            "handovers_completed": completed,
            "handovers_remaining": len(remaining),
            "audit_events": audit_events,
            "audit_valid": audit_valid,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = (result["status"] == "ok" and result["audit_valid"]
          and result["handovers_remaining"] == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
