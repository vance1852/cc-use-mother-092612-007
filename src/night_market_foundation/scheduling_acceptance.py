"""运行协同台账的离线端到端验收。

覆盖第二届中医文化夜市排班负责人关心的完整链路：台账登记、班次版本、
签到事实幂等、缺员替岗方案与确认、紧急接管与期限失效、岗位履职视图、
服务重启后接续未完成的交接，以及审计链校验。
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import ManualClock
from .errors import ConflictError, PermissionDenied
from .scheduling import SchedulingService
from .storage import Database

CST = timezone(timedelta(hours=8))


def _cst(hour: int, minute: int = 0) -> str:
    return f"2026-09-27T{hour:02d}:{minute:02d}:00+08:00"


def run() -> dict[str, object]:
    """执行一条完整排班协同链并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "scheduling.sqlite3"
        clock = ManualClock(datetime(2026, 9, 27, 9, 0, tzinfo=CST))
        database = Database(path)
        service = SchedulingService(database, clock)

        # 基础登记：机构、操作者、站点。
        service.register_organization(request_id="acc-org", actor_id="bootstrap",
                                      organization_id="org-night", name="中医文化夜市组委会")
        service.register_actor(request_id="acc-admin", actor_id="bootstrap", new_actor_id="admin-1",
                               display_name="系统管理员", role="admin", organization_id="org-night")
        service.register_actor(request_id="acc-lead", actor_id="admin-1", new_actor_id="lead-1",
                               display_name="排班负责人", role="operator", organization_id="org-night")
        service.register_actor(request_id="acc-coord", actor_id="admin-1", new_actor_id="coord-1",
                               display_name="现场协调员", role="reviewer", organization_id="org-night")
        service.register_actor(request_id="acc-auditor", actor_id="admin-1", new_actor_id="audit-1",
                               display_name="审计员", role="auditor", organization_id="org-night")
        service.register_site(request_id="acc-site", actor_id="lead-1", site_id="site-night",
                              organization_id="org-night", name="夜市主会场",
                              timezone_name="Asia/Shanghai")

        # 台账登记：专区、人员、资质、可服务时间。
        service.register_zone(request_id="acc-zone", actor_id="lead-1", site_id="site-night",
                              zone_id="zone-clinic", name="义诊专区")
        roster = [
            ("p-wang", "王医师", "doctor", ["consultation"]),
            ("p-li", "李护士", "nurse", ["escort", "nursing"]),
            ("p-chen", "陈护士", "nurse", ["escort"]),
            ("p-zhao", "赵后勤", "logistics", ["supply"]),
            ("p-zhang", "张志愿", "volunteer", ["order"]),
        ]
        for index, (pid, name, role, skills) in enumerate(roster):
            service.register_participant(request_id=f"acc-p-{pid}", actor_id="lead-1",
                                         site_id="site-night", participant_id=pid, name=name,
                                         role_type=role,
                                         contact={"phone": f"1380000000{index}"})
            for skill in skills:
                service.register_qualification(
                    request_id=f"acc-q-{pid}-{skill}", actor_id="lead-1", participant_id=pid,
                    qualification_id=f"q-{pid}-{skill}", skill=skill,
                    certificate_no=f"ZY-2026-{pid}", valid_from="2026-09-01T00:00:00+08:00",
                    valid_until="2026-10-31T23:59:00+08:00")
            service.register_availability(
                request_id=f"acc-a-{pid}", actor_id="lead-1", participant_id=pid,
                windows=[{"start": _cst(17), "end": _cst(22)}])

        # 班次与岗位、岗位依赖。
        service.create_shift(
            request_id="acc-shift", actor_id="lead-1", shift_id="shift-1", zone_id="zone-clinic",
            start_ts=_cst(18), end_ts=_cst(21),
            positions=[
                {"position_id": "pos-consult", "title": "名中医坐诊", "skill": "consultation",
                 "min_staff": 1, "responsible": True},
                {"position_id": "pos-escort", "title": "义诊陪同", "skill": "escort",
                 "min_staff": 1},
                {"position_id": "pos-supply", "title": "物资补给", "skill": "supply",
                 "min_staff": 1},
                {"position_id": "pos-order", "title": "秩序维护", "skill": "order",
                 "min_staff": 1},
            ],
            dependencies=[{"position_id": "pos-escort", "requires_position_id": "pos-consult",
                           "note": "陪同需名中医在岗"}])
        for req, position_id, pid in [("acc-as-1", "pos-consult", "p-wang"),
                                      ("acc-as-2", "pos-escort", "p-li"),
                                      ("acc-as-3", "pos-supply", "p-zhao"),
                                      ("acc-as-4", "pos-order", "p-zhang")]:
            service.create_assignment(request_id=req, actor_id="lead-1", shift_id="shift-1",
                                      position_id=position_id, participant_id=pid)

        # 接口问答：指定时刻的区域责任人与未覆盖依赖。
        responsible = service.zone_responsible(actor_id="lead-1", zone_id="zone-clinic",
                                               at=_cst(19))
        uncovered = service.uncovered_dependencies(actor_id="lead-1", shift_id="shift-1")

        # 签到事实：重复签到与迟到回执保持幂等。
        first = service.record_checkin(request_id="acc-ci-1", actor_id="lead-1",
                                       shift_id="shift-1", participant_id="p-li",
                                       kind="checkin", occurred_at=_cst(18, 5))
        duplicate = service.record_checkin(request_id="acc-ci-2", actor_id="lead-1",
                                           shift_id="shift-1", participant_id="p-li",
                                           kind="checkin", occurred_at=_cst(18, 5))
        service.record_checkin(request_id="acc-ci-3", actor_id="lead-1", shift_id="shift-1",
                               participant_id="p-li", kind="early_leave",
                               occurred_at=_cst(19, 30), note="家中有事提前离场")
        checkins = service.list_checkins(actor_id="lead-1", shift_id="shift-1")

        # 缺员替岗：生成多种选择，负责人确认完整方案后才更新排班。
        clock.set(datetime(2026, 9, 27, 19, 35, tzinfo=CST))
        plan = service.generate_replacement_plan(
            request_id="acc-plan", actor_id="lead-1", shift_id="shift-1",
            participant_id="p-li", reason="李护士提前离场", window_start=_cst(19, 30))
        options = service.list_replacement_plans(actor_id="lead-1",
                                                 plan_id=plan.resource_id)[0]["options"]

        # 拒绝调度可查询具体原因：张志愿没有坐诊资质。
        rejected = False
        try:
            service.create_assignment(request_id="acc-bad", actor_id="lead-1",
                                      shift_id="shift-1", position_id="pos-consult",
                                      participant_id="p-zhang")
        except ConflictError:
            rejected = True
        rejections = service.dispatch_logs(actor_id="lead-1", shift_id="shift-1",
                                           result="rejected")

        # 紧急接管：登记原因、有效期限和交接事项，接管人确认替岗方案。
        service.register_takeover(request_id="acc-take", actor_id="lead-1",
                                  takeover_id="take-1", shift_id="shift-1",
                                  grantee_actor_id="coord-1",
                                  reason="负责人临时处理入口拥堵",
                                  valid_until=_cst(20, 30),
                                  handover_items=["核对义诊区剩余物资", "同步讲解岗排队情况"])
        service.confirm_plan_option(request_id="acc-confirm", actor_id="coord-1",
                                    plan_id=plan.resource_id,
                                    option_id=options[0]["option_id"])
        detail = service.get_shift(actor_id="lead-1", shift_id="shift-1")

        # 期限届满后自动失去调度权。
        clock.set(datetime(2026, 9, 27, 21, 0, tzinfo=CST))
        lost = False
        try:
            service.create_assignment(request_id="acc-late", actor_id="coord-1",
                                      shift_id="shift-1", position_id="pos-order",
                                      participant_id="p-chen")
        except PermissionDenied:
            lost = True

        # 岗位履职视图：各岗位只能读取履职必需的参与者信息。
        order_view = service.participants_view(actor_id="coord-1", shift_id="shift-1",
                                               view="order")
        escort_view = service.participants_view(actor_id="coord-1", shift_id="shift-1",
                                                view="escort")
        auditor_blocked = False
        try:
            service.participants_view(actor_id="audit-1", shift_id="shift-1", view="order")
        except PermissionDenied:
            auditor_blocked = True

        # 服务恢复：重启数据库后接续尚未完成的交接。
        database.close()
        database = Database(path)
        service = SchedulingService(database, clock)
        pending = service.pending_handovers(actor_id="lead-1")
        resumed = len(pending) == 1 and len(pending[0]["pending_items"]) == 2
        for index, item in enumerate(pending[0]["pending_items"]):
            service.complete_handover_item(request_id=f"acc-ho-{index}", actor_id="coord-1",
                                           takeover_id="take-1", item_id=item["item_id"])
        handovers_done = service.pending_handovers(actor_id="lead-1") == []

        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "shift_version": detail["version"],
            "responsible": [item["participant_id"] for item in responsible["responsible"]],
            "uncovered_dependencies": len(uncovered["uncovered"]),
            "checkin_rows": len(checkins),
            "checkin_deduplicated": first.resource_id == duplicate.resource_id,
            "plan_options": len(options),
            "rejection_logged": rejected and len(rejections) > 0,
            "takeover_expired": lost,
            "order_view_least_privilege": all("phone" not in item for item in order_view),
            "escort_view_has_phone": all("phone" in item for item in escort_view),
            "auditor_blocked": auditor_blocked,
            "handovers_resumed": resumed,
            "handovers_done": handovers_done,
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    ok = result["status"] == "ok" and all(value for key, value in result.items()
                                          if key not in {"status", "audit_events",
                                                         "shift_version", "checkin_rows",
                                                         "plan_options", "responsible",
                                                         "uncovered_dependencies"})
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
