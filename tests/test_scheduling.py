import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from night_market_foundation.clock import ManualClock
from night_market_foundation.errors import (
    ConflictError,
    PermissionDenied,
    ValidationError,
)
from night_market_foundation.scheduling import SchedulingService
from night_market_foundation.storage import Database

CST = timezone(timedelta(hours=8))
SHIFT_START = "2026-09-27T18:00:00+08:00"
SHIFT_END = "2026-09-27T21:00:00+08:00"
QUAL_FROM = "2026-09-01T00:00:00+08:00"
QUAL_UNTIL = "2026-10-31T23:59:00+08:00"


class SchedulingTestBase(unittest.TestCase):
    """搭建一个完整的夜市排班台账场景。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "scheduling.sqlite3"
        self.database = Database(self.path)
        self.clock = ManualClock(datetime(2026, 9, 27, 9, 0, tzinfo=CST))
        self.service = SchedulingService(self.database, self.clock)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="组委会")
        self.service.register_actor(request_id="admin", actor_id="bootstrap",
                                    new_actor_id="a1", display_name="管理员",
                                    role="admin", organization_id="o1")
        self.service.register_actor(request_id="lead", actor_id="a1", new_actor_id="lead",
                                    display_name="排班负责人", role="operator",
                                    organization_id="o1")
        self.service.register_actor(request_id="coord", actor_id="a1", new_actor_id="coord",
                                    display_name="现场协调员", role="reviewer",
                                    organization_id="o1")
        self.service.register_actor(request_id="coord2", actor_id="a1", new_actor_id="coord2",
                                    display_name="另一协调员", role="reviewer",
                                    organization_id="o1")
        self.service.register_actor(request_id="auditor", actor_id="a1", new_actor_id="au1",
                                    display_name="审计员", role="auditor", organization_id="o1")
        self.service.register_site(request_id="site", actor_id="lead", site_id="s1",
                                   organization_id="o1", name="主会场",
                                   timezone_name="Asia/Shanghai")
        self.service.register_zone(request_id="zone", actor_id="lead", site_id="s1",
                                   zone_id="z1", name="义诊专区")
        self._participants()
        self._shift()
        self._assignments()

    def tearDown(self):
        self.database.close()
        self.tmp.cleanup()

    def reopen(self):
        """模拟服务重启：关闭并用同一文件重新打开数据库。"""

        self.database.close()
        self.database = Database(self.path)
        self.service = SchedulingService(self.database, self.clock)

    def _add_participant(self, pid, name, role, skills, phone, quals_valid=None, windows=None):
        self.service.register_participant(request_id=f"p-{pid}", actor_id="lead", site_id="s1",
                                          participant_id=pid, name=name, role_type=role,
                                          contact={"phone": phone})
        for skill in skills:
            valid_from, valid_until = (quals_valid or {}).get(skill, (QUAL_FROM, QUAL_UNTIL))
            self.service.register_qualification(
                request_id=f"q-{pid}-{skill}", actor_id="lead", participant_id=pid,
                qualification_id=f"q-{pid}-{skill}", skill=skill,
                certificate_no=f"证-{pid}-{skill}", valid_from=valid_from,
                valid_until=valid_until)
        self.service.register_availability(
            request_id=f"av-{pid}", actor_id="lead", participant_id=pid,
            windows=windows or [{"start": "2026-09-27T17:00:00+08:00",
                                 "end": "2026-09-27T22:00:00+08:00"}])

    def _participants(self):
        self._add_participant("p-wang", "王医师", "doctor", ["consultation"], "13800000001")
        self._add_participant("p-li", "李护士", "nurse", ["escort", "nursing"], "13800000002")
        self._add_participant("p-chen", "陈护士", "nurse", ["escort", "supply"], "13800000003")
        self._add_participant("p-zhao", "赵后勤", "logistics", ["supply", "escort"], "13800000004")
        self._add_participant("p-qian", "钱后勤", "logistics", ["supply", "order"], "13800000006")
        self._add_participant("p-zhang", "张志愿", "volunteer", ["order"], "13800000005")
        self._add_participant("p-exp", "过期护士", "nurse", ["escort"], "13800000007",
                              quals_valid={"escort": ("2026-08-01T00:00:00+08:00",
                                                      "2026-09-20T23:59:00+08:00")})
        self._add_participant("p-part", "短时护士", "nurse", ["escort"], "13800000008",
                              windows=[{"start": "2026-09-27T17:00:00+08:00",
                                        "end": "2026-09-27T19:00:00+08:00"}])

    def _shift(self):
        self.service.create_shift(
            request_id="shift", actor_id="lead", shift_id="sh1", zone_id="z1",
            start_ts=SHIFT_START, end_ts=SHIFT_END,
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

    def _assignments(self):
        for req, position_id, pid in [("as1", "pos-consult", "p-wang"),
                                      ("as2", "pos-escort", "p-li"),
                                      ("as3", "pos-supply", "p-zhao"),
                                      ("as4", "pos-supply", "p-qian"),
                                      ("as5", "pos-order", "p-zhang")]:
            self.service.create_assignment(request_id=req, actor_id="lead", shift_id="sh1",
                                           position_id=position_id, participant_id=pid)

    def _plan_options(self, plan_id):
        return self.service.list_replacement_plans(actor_id="lead", plan_id=plan_id)[0]["options"]

    @staticmethod
    def _option_for(options, participant_id):
        return next(option for option in options
                    if any(change["action"] == "add" and change["participant_id"] == participant_id
                           for change in option["changes"]))


class DispatchRejectionTest(SchedulingTestBase):
    def test_invalid_dispatches_are_rejected_with_reasons(self):
        with self.assertRaises(ConflictError):
            # 资质过期
            self.service.create_assignment(request_id="bad1", actor_id="lead", shift_id="sh1",
                                           position_id="pos-escort", participant_id="p-exp")
        with self.assertRaises(ConflictError):
            # 没有坐诊资质
            self.service.create_assignment(request_id="bad2", actor_id="lead", shift_id="sh1",
                                           position_id="pos-consult", participant_id="p-zhang")
        with self.assertRaises(ConflictError):
            # 可服务时间覆盖不了整个班次
            self.service.create_assignment(request_id="bad3", actor_id="lead", shift_id="sh1",
                                           position_id="pos-escort", participant_id="p-part")
        with self.assertRaises(ConflictError):
            # 王医师整个班次都在坐诊，时间冲突
            self.service.create_assignment(request_id="bad4", actor_id="lead", shift_id="sh1",
                                           position_id="pos-order", participant_id="p-wang")
        logs = self.service.dispatch_logs(actor_id="lead", shift_id="sh1", result="rejected")
        codes = [reason["code"] for log in logs for reason in log["reasons"]]
        self.assertIn("QUALIFICATION_EXPIRED", codes)
        self.assertIn("QUALIFICATION_MISSING", codes)
        self.assertIn("AVAILABILITY_MISSING", codes)
        self.assertIn("TIME_CONFLICT", codes)
        # 拒绝不会留下半套变更：排班保持 5 条已确认安排
        detail = self.service.get_shift(actor_id="lead", shift_id="sh1")
        self.assertEqual(5, len(detail["assignments"]))
        self.assertEqual(6, detail["version"])

    def test_request_replay_and_payload_conflict(self):
        first = self.service.register_participant(request_id="rp1", actor_id="lead", site_id="s1",
                                                  participant_id="p-new", name="新人员",
                                                  role_type="volunteer")
        replay = self.service.register_participant(request_id="rp1", actor_id="lead", site_id="s1",
                                                   participant_id="p-new", name="新人员",
                                                   role_type="volunteer")
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        with self.assertRaises(ConflictError):
            self.service.register_participant(request_id="rp1", actor_id="lead", site_id="s1",
                                              participant_id="p-new", name="改名",
                                              role_type="volunteer")

    def test_write_permissions(self):
        with self.assertRaises(PermissionDenied):
            self.service.register_participant(request_id="x1", actor_id="coord", site_id="s1",
                                              participant_id="p-x", name="路人",
                                              role_type="volunteer")
        with self.assertRaises(PermissionDenied):
            self.service.create_assignment(request_id="x2", actor_id="coord", shift_id="sh1",
                                           position_id="pos-order", participant_id="p-chen")
        with self.assertRaises(PermissionDenied):
            self.service.generate_replacement_plan(request_id="x3", actor_id="au1",
                                                   shift_id="sh1", participant_id="p-li",
                                                   reason="缺员")


class CheckinTest(SchedulingTestBase):
    def test_duplicate_checkin_and_late_receipt_are_idempotent(self):
        first = self.service.record_checkin(request_id="c1", actor_id="lead", shift_id="sh1",
                                            participant_id="p-wang", kind="checkin",
                                            occurred_at="2026-09-27T18:05:00+08:00")
        replay = self.service.record_checkin(request_id="c1", actor_id="lead", shift_id="sh1",
                                             participant_id="p-wang", kind="checkin",
                                             occurred_at="2026-09-27T18:05:00+08:00")
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        duplicate = self.service.record_checkin(request_id="c2", actor_id="lead", shift_id="sh1",
                                                participant_id="p-wang", kind="checkin",
                                                occurred_at="2026-09-27T18:05:00+08:00")
        self.assertEqual(first.resource_id, duplicate.resource_id)
        self.assertEqual(1, len(self.service.list_checkins(actor_id="lead", shift_id="sh1")))
        late1 = self.service.record_checkin(request_id="c3", actor_id="lead", shift_id="sh1",
                                            participant_id="p-li", kind="late",
                                            occurred_at="2026-09-27T18:20:00+08:00",
                                            note="地铁晚点")
        late2 = self.service.record_checkin(request_id="c4", actor_id="lead", shift_id="sh1",
                                            participant_id="p-li", kind="late",
                                            occurred_at="2026-09-27T18:20:00+08:00",
                                            note="地铁晚点")
        self.assertEqual(late1.resource_id, late2.resource_id)
        checkins = self.service.list_checkins(actor_id="lead", shift_id="sh1")
        self.assertEqual(2, len(checkins))
        self.assertEqual("late", checkins[1]["kind"])

    def test_checkin_requires_assignment(self):
        with self.assertRaises(ValidationError):
            self.service.record_checkin(request_id="c9", actor_id="lead", shift_id="sh1",
                                        participant_id="p-chen", kind="checkin",
                                        occurred_at="2026-09-27T18:05:00+08:00")

    def test_checkin_kind_must_be_valid(self):
        with self.assertRaises(ValidationError):
            self.service.record_checkin(request_id="c8", actor_id="lead", shift_id="sh1",
                                        participant_id="p-wang", kind="undo",
                                        occurred_at="2026-09-27T18:05:00+08:00")


class ReplacementPlanTest(SchedulingTestBase):
    def test_plan_options_carry_impacts_and_confirm_updates_roster(self):
        self.service.record_checkin(request_id="leave", actor_id="lead", shift_id="sh1",
                                    participant_id="p-li", kind="early_leave",
                                    occurred_at="2026-09-27T19:30:00+08:00")
        self.clock.set(datetime(2026, 9, 27, 19, 35, tzinfo=CST))
        plan = self.service.generate_replacement_plan(
            request_id="plan1", actor_id="lead", shift_id="sh1", participant_id="p-li",
            reason="李护士提前离场", window_start="2026-09-27T19:30:00+08:00")
        options = self._plan_options(plan.resource_id)
        self.assertTrue(options)
        chen = self._option_for(options, "p-chen")
        self.assertTrue(chen["impact"])
        self.assertTrue(any("连带影响" in note for note in chen["impact"]))
        receipt = self.service.confirm_plan_option(request_id="confirm1", actor_id="lead",
                                                   plan_id=plan.resource_id,
                                                   option_id=chen["option_id"])
        self.assertFalse(receipt.replayed)
        replay = self.service.confirm_plan_option(request_id="confirm1", actor_id="lead",
                                                  plan_id=plan.resource_id,
                                                  option_id=chen["option_id"])
        self.assertTrue(replay.replayed)
        detail = self.service.get_shift(actor_id="lead", shift_id="sh1")
        self.assertEqual(7, detail["version"])
        escort = sorted((a["participant_id"], a["start_ts"], a["end_ts"])
                        for a in detail["assignments"] if a["position_id"] == "pos-escort")
        self.assertIn(("p-li", "2026-09-27T10:00:00Z", "2026-09-27T11:30:00Z"), escort)
        self.assertIn(("p-chen", "2026-09-27T11:30:00Z", "2026-09-27T13:00:00Z"), escort)
        revision = self.service.shift_revision(actor_id="lead", shift_id="sh1", version=7)
        self.assertTrue(any(a["participant_id"] == "p-chen" for a in revision["assignments"]))
        plan_after = self.service.list_replacement_plans(actor_id="lead",
                                                         plan_id=plan.resource_id)[0]
        self.assertEqual("confirmed", plan_after["status"])
        self.assertEqual("lead", plan_after["confirmed_by"])

    def test_late_cover_keeps_original_assignment_and_split_option_exists(self):
        plan = self.service.generate_replacement_plan(
            request_id="late-plan", actor_id="lead", shift_id="sh1", participant_id="p-li",
            reason="李护士迟到", window_start="2026-09-27T18:00:00+08:00",
            window_end="2026-09-27T18:40:00+08:00")
        options = self._plan_options(plan.resource_id)
        chen = self._option_for(options, "p-chen")
        self.assertFalse(any(change["action"] in ("release", "shorten")
                             for change in chen["changes"]))
        self.assertTrue(any("临时顶岗" in note for note in chen["impact"]))
        split = next((option for option in options
                      if sum(1 for change in option["changes"] if change["action"] == "add") == 2),
                     None)
        self.assertIsNotNone(split)
        self.assertTrue(any("分段覆盖" in note for note in split["impact"]))
        self.service.confirm_plan_option(request_id="late-confirm", actor_id="lead",
                                         plan_id=plan.resource_id, option_id=chen["option_id"])
        detail = self.service.get_shift(actor_id="lead", shift_id="sh1")
        escort = sorted((a["participant_id"], a["start_ts"], a["end_ts"])
                        for a in detail["assignments"] if a["position_id"] == "pos-escort")
        self.assertIn(("p-li", "2026-09-27T10:00:00Z", "2026-09-27T13:00:00Z"), escort)
        self.assertIn(("p-chen", "2026-09-27T10:00:00Z", "2026-09-27T10:40:00Z"), escort)

    def test_confirm_is_atomic_when_min_staffing_would_break(self):
        # 李护士缺席：方案含“借调赵后勤”（补给岗 2→1，合规）。
        plan_a = self.service.generate_replacement_plan(
            request_id="pa", actor_id="lead", shift_id="sh1", participant_id="p-li",
            reason="李护士缺席")
        options_a = self._plan_options(plan_a.resource_id)
        zhao_assignment = next(a for a in self.service.get_shift(actor_id="lead", shift_id="sh1")["assignments"]
                               if a["participant_id"] == "p-zhao")
        borrow_zhao = next(option for option in options_a
                           if any(change["action"] == "release"
                                  and change["assignment_id"] == zhao_assignment["assignment_id"]
                                  for change in option["changes"]))
        self.assertTrue(any("借调" in note for note in borrow_zhao["impact"]))
        # 张志愿也缺席：方案含“借调钱后勤”（补给岗 2→1，合规）。两个方案各自生成都合法。
        plan_c = self.service.generate_replacement_plan(
            request_id="pc", actor_id="lead", shift_id="sh1", participant_id="p-zhang",
            reason="张志愿缺席")
        options_c = self._plan_options(plan_c.resource_id)
        qian_assignment = next(a for a in self.service.get_shift(actor_id="lead", shift_id="sh1")["assignments"]
                               if a["participant_id"] == "p-qian")
        borrow_qian = next(option for option in options_c
                           if any(change["action"] == "release"
                                  and change["assignment_id"] == qian_assignment["assignment_id"]
                                  for change in option["changes"]))
        # 先确认借调赵后勤：补给岗只剩钱后勤一人，仍满足最低在岗数。
        self.service.confirm_plan_option(request_id="ca", actor_id="lead",
                                         plan_id=plan_a.resource_id,
                                         option_id=borrow_zhao["option_id"])
        # 再确认借调钱后勤会把补给岗掏空：整体拒绝，不留半套变更。
        with self.assertRaises(ConflictError) as ctx:
            self.service.confirm_plan_option(request_id="cc", actor_id="lead",
                                             plan_id=plan_c.resource_id,
                                             option_id=borrow_qian["option_id"])
        self.assertIn("最低", str(ctx.exception))
        detail = self.service.get_shift(actor_id="lead", shift_id="sh1")
        self.assertEqual(7, detail["version"])
        escort = [a for a in detail["assignments"] if a["position_id"] == "pos-escort"]
        self.assertEqual(["p-zhao"], [a["participant_id"] for a in escort])
        supply = [a for a in detail["assignments"] if a["position_id"] == "pos-supply"]
        self.assertEqual(["p-qian"], [a["participant_id"] for a in supply])
        order = [a for a in detail["assignments"] if a["position_id"] == "pos-order"]
        self.assertEqual(["p-zhang"], [a["participant_id"] for a in order])
        self.assertEqual("2026-09-27T13:00:00Z", order[0]["end_ts"])
        logs = self.service.dispatch_logs(actor_id="lead", shift_id="sh1", result="rejected")
        self.assertTrue(any(reason["code"] == "MIN_STAFFING"
                            for log in logs for reason in log["reasons"]))
        plan_c_after = self.service.list_replacement_plans(actor_id="lead",
                                                           plan_id=plan_c.resource_id)[0]
        self.assertEqual("proposed", plan_c_after["status"])

    def test_stale_option_time_conflict_is_rejected_atomically(self):
        plan = self.service.generate_replacement_plan(
            request_id="stale", actor_id="lead", shift_id="sh1", participant_id="p-li",
            reason="李护士缺席")
        chen = self._option_for(self._plan_options(plan.resource_id), "p-chen")
        # 方案生成后陈护士被安排到补给岗，与原选项冲突。
        self.service.create_assignment(request_id="other", actor_id="lead", shift_id="sh1",
                                       position_id="pos-supply", participant_id="p-chen",
                                       start_ts="2026-09-27T18:00:00+08:00",
                                       end_ts="2026-09-27T21:00:00+08:00")
        with self.assertRaises(ConflictError):
            self.service.confirm_plan_option(request_id="stale-c", actor_id="lead",
                                             plan_id=plan.resource_id,
                                             option_id=chen["option_id"])
        detail = self.service.get_shift(actor_id="lead", shift_id="sh1")
        escort = [a for a in detail["assignments"] if a["position_id"] == "pos-escort"]
        self.assertEqual(["p-li"], [a["participant_id"] for a in escort])
        logs = self.service.dispatch_logs(actor_id="lead", shift_id="sh1", result="rejected")
        self.assertTrue(any(reason["code"] == "TIME_CONFLICT"
                            for log in logs for reason in log["reasons"]))

    def test_generation_logs_candidate_rejections(self):
        plan = self.service.generate_replacement_plan(
            request_id="gen", actor_id="lead", shift_id="sh1", participant_id="p-li",
            reason="李护士缺席")
        self.assertTrue(self._plan_options(plan.resource_id))
        logs = [log for log in self.service.dispatch_logs(actor_id="lead", shift_id="sh1",
                                                          result="rejected")
                if log["action"] == "generate"]
        codes = {reason["code"] for log in logs for reason in log["reasons"]}
        # 张志愿无陪同资质、过期护士资质过期、短时护士时间覆盖不足。
        self.assertIn("QUALIFICATION_MISSING", codes)
        self.assertIn("QUALIFICATION_EXPIRED", codes)
        self.assertIn("AVAILABILITY_MISSING", codes)


class TakeoverTest(SchedulingTestBase):
    def test_takeover_grants_and_loses_dispatch_authority(self):
        self.service.register_takeover(
            request_id="t1", actor_id="lead", takeover_id="tk1", shift_id="sh1",
            grantee_actor_id="coord", reason="负责人临时离场",
            valid_until="2026-09-27T20:00:00+08:00",
            handover_items=["核对物资台账", "交接重点患者陪同注意事项"])
        # 接管人在有效期内可以登记事实、生成并确认完整方案。
        self.service.record_checkin(request_id="lv", actor_id="coord", shift_id="sh1",
                                    participant_id="p-li", kind="early_leave",
                                    occurred_at="2026-09-27T19:30:00+08:00")
        plan = self.service.generate_replacement_plan(
            request_id="gp", actor_id="coord", shift_id="sh1", participant_id="p-li",
            reason="提前离场", window_start="2026-09-27T19:30:00+08:00")
        option = self._plan_options(plan.resource_id)[0]
        self.service.confirm_plan_option(request_id="cf", actor_id="coord",
                                         plan_id=plan.resource_id,
                                         option_id=option["option_id"])
        # 期限届满后自动失去调度权。
        self.clock.set(datetime(2026, 9, 27, 20, 30, tzinfo=CST))
        with self.assertRaises(PermissionDenied):
            self.service.create_assignment(request_id="late", actor_id="coord", shift_id="sh1",
                                           position_id="pos-order", participant_id="p-chen")
        takeovers = self.service.list_takeovers(actor_id="lead", shift_id="sh1")
        self.assertEqual("expired", takeovers[0]["status"])

    def test_handover_resume_after_restart(self):
        self.service.register_takeover(
            request_id="t1", actor_id="lead", takeover_id="tk1", shift_id="sh1",
            grantee_actor_id="coord", reason="负责人临时离场",
            valid_until="2026-09-27T20:00:00+08:00",
            handover_items=["核对物资台账", "交接重点患者陪同注意事项"])
        self.clock.set(datetime(2026, 9, 27, 21, 0, tzinfo=CST))
        self.reopen()
        pending = self.service.pending_handovers(actor_id="lead")
        self.assertEqual(1, len(pending))
        self.assertEqual("expired", pending[0]["status"])
        self.assertEqual(2, len(pending[0]["pending_items"]))
        first, second = pending[0]["pending_items"]
        # 非接管人也非负责人不能登记交接完成。
        with self.assertRaises(PermissionDenied):
            self.service.complete_handover_item(request_id="h0", actor_id="coord2",
                                                takeover_id="tk1", item_id=first["item_id"])
        # 接管虽已过期，交接事项仍可接续完成；重复完成保持幂等。
        self.service.complete_handover_item(request_id="h1", actor_id="coord",
                                            takeover_id="tk1", item_id=first["item_id"])
        again = self.service.complete_handover_item(request_id="h2", actor_id="coord",
                                                    takeover_id="tk1", item_id=first["item_id"])
        self.assertEqual(first["item_id"], again.resource_id)
        pending = self.service.pending_handovers(actor_id="lead")
        self.assertEqual(1, len(pending[0]["pending_items"]))
        self.service.complete_handover_item(request_id="h3", actor_id="lead",
                                            takeover_id="tk1", item_id=second["item_id"])
        self.assertEqual([], self.service.pending_handovers(actor_id="lead"))
        takeovers = self.service.list_takeovers(actor_id="lead", shift_id="sh1")
        self.assertEqual("completed", takeovers[0]["status"])
        valid, _ = self.service.verify_audit()
        self.assertTrue(valid)

    def test_takeover_requires_future_validity(self):
        with self.assertRaises(ValidationError):
            self.service.register_takeover(
                request_id="t9", actor_id="lead", takeover_id="tk9", shift_id="sh1",
                grantee_actor_id="coord", reason="测试",
                valid_until="2026-09-27T08:00:00+08:00", handover_items=["事项"])


class QueryTest(SchedulingTestBase):
    def test_zone_responsible_and_uncovered_dependencies(self):
        uncovered = self.service.uncovered_dependencies(actor_id="lead", shift_id="sh1")
        self.assertEqual([], uncovered["uncovered"])
        responsible = self.service.zone_responsible(actor_id="lead", zone_id="z1",
                                                    at="2026-09-27T19:00:00+08:00")
        self.assertEqual("sh1", responsible["shift_id"])
        self.assertEqual(["p-wang"], [item["participant_id"]
                                      for item in responsible["responsible"]])
        # 次日班次：陪同在岗但名中医未排，依赖未覆盖；补齐后覆盖。
        self.service.create_shift(
            request_id="sh2", actor_id="lead", shift_id="sh2", zone_id="z1",
            start_ts="2026-09-28T18:00:00+08:00", end_ts="2026-09-28T21:00:00+08:00",
            positions=[
                {"position_id": "s2-consult", "title": "名中医坐诊", "skill": "consultation",
                 "min_staff": 1, "responsible": True},
                {"position_id": "s2-escort", "title": "义诊陪同", "skill": "escort",
                 "min_staff": 1},
            ],
            dependencies=[{"position_id": "s2-escort", "requires_position_id": "s2-consult",
                           "note": "陪同需名中医在岗"}])
        for pid in ("p-li", "p-wang"):
            self.service.register_availability(
                request_id=f"av2-{pid}", actor_id="lead", participant_id=pid,
                windows=[{"start": "2026-09-28T17:00:00+08:00",
                          "end": "2026-09-28T22:00:00+08:00"}])
        self.service.create_assignment(request_id="s2a", actor_id="lead", shift_id="sh2",
                                       position_id="s2-escort", participant_id="p-li")
        uncovered = self.service.uncovered_dependencies(actor_id="lead", shift_id="sh2")
        self.assertEqual(1, len(uncovered["uncovered"]))
        self.assertEqual("名中医坐诊", uncovered["items"][0]["requires_title"])
        self.service.create_assignment(request_id="s2b", actor_id="lead", shift_id="sh2",
                                       position_id="s2-consult", participant_id="p-wang")
        self.assertEqual([], self.service.uncovered_dependencies(actor_id="lead",
                                                                 shift_id="sh2")["uncovered"])
        responsible = self.service.zone_responsible(actor_id="lead", zone_id="z1",
                                                    at="2026-09-28T19:00:00+08:00")
        self.assertEqual("sh2", responsible["shift_id"])
        empty = self.service.zone_responsible(actor_id="lead", zone_id="z1",
                                              at="2026-09-29T12:00:00+08:00")
        self.assertIsNone(empty["shift_id"])
        self.assertEqual([], empty["responsible"])

    def test_shift_versions_keep_confirmed_history(self):
        detail = self.service.get_shift(actor_id="lead", shift_id="sh1")
        self.assertEqual(6, detail["version"])
        self.assertEqual([1, 2, 3, 4, 5, 6], detail["versions"])
        first = self.service.shift_revision(actor_id="lead", shift_id="sh1", version=1)
        self.assertEqual([], first["assignments"])
        latest = self.service.shift_revision(actor_id="lead", shift_id="sh1", version=6)
        self.assertEqual(5, len(latest["assignments"]))
        self.assertEqual(4, len(latest["positions"]))

    def test_participants_view_is_least_privilege(self):
        full = self.service.participants_view(actor_id="lead", shift_id="sh1")
        self.assertIn("contact", full[0])
        self.assertIn("qualifications", full[0])
        escort = self.service.participants_view(actor_id="coord", shift_id="sh1", view="escort")
        self.assertTrue(escort)
        self.assertTrue(all("phone" in item for item in escort))
        self.assertTrue(all("contact" not in item for item in escort))
        supply = self.service.participants_view(actor_id="coord", shift_id="sh1", view="supply")
        self.assertTrue(all("positions" in item and "phone" not in item for item in supply))
        order = self.service.participants_view(actor_id="coord", shift_id="sh1", view="order")
        self.assertTrue(all(set(item) == {"participant_id", "name", "role_type"}
                            for item in order))
        with self.assertRaises(ValidationError):
            self.service.participants_view(actor_id="coord", shift_id="sh1")
        with self.assertRaises(PermissionDenied):
            self.service.participants_view(actor_id="au1", shift_id="sh1", view="order")

    def test_reads_require_ledger_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.get_shift(actor_id="au1", shift_id="sh1")
        with self.assertRaises(PermissionDenied):
            self.service.dispatch_logs(actor_id="au1", shift_id="sh1")


if __name__ == "__main__":
    unittest.main()
