import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from night_market_foundation.clock import FixedClock
from night_market_foundation.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database
from night_market_scheduling.service import PlanRejected, SchedulingService


SHIFT_START = "2026-10-01T10:00:00Z"
SHIFT_END = "2026-10-01T14:00:00Z"


class SchedulingTestBase(unittest.TestCase):
    """准备一套完整的专区、岗位、人员与班次台账。"""

    clock_at = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.database = Database()
        self.foundation = DomainService(self.database, FixedClock(self.clock_at))
        self.service = SchedulingService(self.database, FixedClock(self.clock_at))
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="夜市组委会")
        self.foundation.register_actor(request_id="admin", actor_id="bootstrap",
                                       new_actor_id="a1", display_name="管理员",
                                       role="admin", organization_id="o1")
        self.foundation.register_actor(request_id="lead", actor_id="a1",
                                       new_actor_id="op1", display_name="排班负责人",
                                       role="operator", organization_id="o1")
        self.foundation.register_actor(request_id="duty", actor_id="a1",
                                       new_actor_id="rv1", display_name="值班调度",
                                       role="reviewer", organization_id="o1")
        self.foundation.register_actor(request_id="auditor", actor_id="a1",
                                       new_actor_id="au1", display_name="审计员",
                                       role="auditor", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="op1", site_id="s1",
                                      organization_id="o1", name="主会场",
                                      timezone_name="Asia/Shanghai")
        self.service.register_zone(request_id="zone", actor_id="op1", zone_id="z1",
                                   site_id="s1", name="义诊专区")
        self.service.register_post(request_id="post-lead", actor_id="op1", post_id="p-lead",
                                   zone_id="z1", name="专区负责人", required_skill="coordination",
                                   min_staff=1, is_responsible=True, necessary_fields=["phone"])
        self.service.register_post(request_id="post-diag", actor_id="op1", post_id="p-diag",
                                   zone_id="z1", name="义诊诊疗", required_skill="diagnosis",
                                   min_staff=1, necessary_fields=["title"])
        self.service.register_post(request_id="post-acc", actor_id="op1", post_id="p-acc",
                                   zone_id="z1", name="义诊陪同", required_skill="accompaniment",
                                   min_staff=1, necessary_fields=["phone"])
        self.service.register_post(request_id="post-sup", actor_id="op1", post_id="p-sup",
                                   zone_id="z1", name="物资补给", required_skill="supply",
                                   min_staff=1, necessary_fields=["phone"])
        self.service.register_post(request_id="post-ord", actor_id="op1", post_id="p-ord",
                                   zone_id="z1", name="秩序维护", required_skill="order_keeping",
                                   min_staff=1)
        for index, depends_on in enumerate(("p-acc", "p-sup", "p-ord")):
            self.service.add_post_dependency(request_id=f"dep-{index}", actor_id="op1",
                                             post_id="p-diag", depends_on_post_id=depends_on)
        # 常规人员：资质与可服务时间都覆盖班次
        for participant_id, role_type, skill in (
                ("doc1", "famous_doctor", "diagnosis"), ("doc2", "famous_doctor", "diagnosis"),
                ("nur1", "nurse", "accompaniment"), ("nur2", "nurse", "accompaniment"),
                ("nur4", "nurse", "accompaniment"), ("nur5", "nurse", "accompaniment"),
                ("log1", "logistics", "supply"), ("log4", "logistics", "supply"),
                ("log2", "logistics", "order_keeping"), ("log5", "logistics", "order_keeping"),
                ("lead1", "logistics", "coordination"), ("lead2", "logistics", "coordination")):
            self._add_participant(participant_id, role_type, skill)
        # 特殊人员：过期资质、缺少资质、可服务时间不足
        self._add_participant("nur3", "nurse", "accompaniment",
                              valid_from="2026-01-01T00:00:00Z", valid_until="2026-06-01T00:00:00Z")
        self._add_participant("vol1", "volunteer_guide", "guiding")
        self._add_participant("log3", "logistics", "supply",
                              avail_start="2026-10-01T08:00:00Z", avail_end="2026-10-01T09:00:00Z")
        self.service.register_shift(request_id="shift-1", actor_id="op1", shift_id="sh1",
                                    zone_id="z1", name="晚班一", start_at=SHIFT_START,
                                    end_at=SHIFT_END)
        self.service.register_shift(request_id="shift-2", actor_id="op1", shift_id="sh2",
                                    zone_id="z1", name="晚班二", start_at="2026-10-01T12:00:00Z",
                                    end_at="2026-10-01T16:00:00Z")

    def _add_participant(self, participant_id, role_type, skill,
                         valid_from="2026-09-01T00:00:00Z", valid_until="2026-12-31T00:00:00Z",
                         avail_start="2026-10-01T08:00:00Z", avail_end="2026-10-01T16:00:00Z"):
        self.service.register_participant(request_id=f"part-{participant_id}", actor_id="op1",
                                          participant_id=participant_id, site_id="s1",
                                          name=f"员工{participant_id}", role_type=role_type,
                                          phone="13800000000", title="")
        self.service.add_qualification(request_id=f"qual-{participant_id}", actor_id="op1",
                                       participant_id=participant_id, skill=skill,
                                       valid_from=valid_from, valid_until=valid_until)
        self.service.add_availability(request_id=f"avail-{participant_id}", actor_id="op1",
                                      participant_id=participant_id,
                                      start_at=avail_start, end_at=avail_end)

    def tearDown(self):
        self.database.close()

    def _full_plan(self, **overrides):
        plan = {"p-lead": "lead1", "p-diag": "doc1", "p-acc": "nur1",
                "p-sup": "log1", "p-ord": "log2"}
        plan.update(overrides)
        return [{"post_id": post_id, "participant_id": participant_id}
                for post_id, participant_id in plan.items()]

    def _confirm_sh1(self, **overrides):
        return self.service.confirm_schedule(request_id="confirm-sh1", actor_id="op1",
                                             shift_id="sh1", assignments=self._full_plan(**overrides))

    def _reasons(self, error):
        return {issue["reason"] for issue in error.issues}

    def _versions(self, shift_id="sh1"):
        return self.service.shift_versions(actor_id="op1", shift_id=shift_id)


class ConfirmScheduleTest(SchedulingTestBase):
    def test_confirm_creates_first_version(self):
        receipt = self._confirm_sh1()
        self.assertFalse(receipt.replayed)
        versions = self._versions()
        self.assertEqual(1, len(versions))
        self.assertEqual("confirmed", versions[0]["status"])
        self.assertEqual(5, versions[0]["assignments"])

    def test_confirm_replay_returns_same_version(self):
        first = self._confirm_sh1()
        second = self._confirm_sh1()
        self.assertTrue(second.replayed)
        self.assertEqual(first.resource_id, second.resource_id)
        self.assertEqual(1, len(self._versions()))

    def test_confirm_rejects_expired_qualification(self):
        with self.assertRaises(PlanRejected) as caught:
            self._confirm_sh1(**{"p-acc": "nur3"})
        self.assertIn("qualification_expired", self._reasons(caught.exception))

    def test_confirm_rejects_missing_qualification(self):
        with self.assertRaises(PlanRejected) as caught:
            self._confirm_sh1(**{"p-acc": "vol1"})
        self.assertIn("qualification_missing", self._reasons(caught.exception))

    def test_confirm_rejects_insufficient_availability(self):
        with self.assertRaises(PlanRejected) as caught:
            self._confirm_sh1(**{"p-sup": "log3"})
        self.assertIn("availability_insufficient", self._reasons(caught.exception))

    def test_confirm_rejects_time_conflict_with_other_shift(self):
        self._confirm_sh1()
        other_plan = self._full_plan(**{"p-lead": "lead2", "p-acc": "nur2",
                                        "p-sup": "log4", "p-ord": "log5"})
        with self.assertRaises(PlanRejected) as caught:
            self.service.confirm_schedule(request_id="confirm-sh2", actor_id="op1",
                                          shift_id="sh2", assignments=other_plan)
        self.assertIn("time_conflict", self._reasons(caught.exception))

    def test_confirm_rejects_min_staff_shortage(self):
        plan = [entry for entry in self._full_plan() if entry["post_id"] != "p-ord"]
        with self.assertRaises(PlanRejected) as caught:
            self.service.confirm_schedule(request_id="confirm-sh1", actor_id="op1",
                                          shift_id="sh1", assignments=plan)
        self.assertIn("min_staff_shortage", self._reasons(caught.exception))

    def test_confirm_rejects_uncovered_dependency(self):
        self.service.register_post(request_id="post-tea", actor_id="op1", post_id="p-tea",
                                   zone_id="z1", name="茶饮引导", required_skill="guiding",
                                   min_staff=0)
        self.service.add_post_dependency(request_id="dep-tea", actor_id="op1",
                                         post_id="p-tea", depends_on_post_id="p-sup")
        plan = [entry for entry in self._full_plan() if entry["post_id"] != "p-sup"]
        plan.append({"post_id": "p-tea", "participant_id": "vol1"})
        with self.assertRaises(PlanRejected) as caught:
            self.service.confirm_schedule(request_id="confirm-sh1", actor_id="op1",
                                          shift_id="sh1", assignments=plan)
        self.assertIn("dependency_uncovered", self._reasons(caught.exception))

    def test_confirm_rejects_duplicate_participant(self):
        plan = self._full_plan(**{"p-ord": "doc1"})
        with self.assertRaises(PlanRejected) as caught:
            self.service.confirm_schedule(request_id="confirm-sh1", actor_id="op1",
                                          shift_id="sh1", assignments=plan)
        self.assertIn("duplicate_in_plan", self._reasons(caught.exception))

    def test_failed_confirm_leaves_no_partial_changes(self):
        plan = [entry for entry in self._full_plan() if entry["post_id"] != "p-ord"]
        with self.assertRaises(PlanRejected):
            self.service.confirm_schedule(request_id="confirm-sh1", actor_id="op1",
                                          shift_id="sh1", assignments=plan)
        versions = self.database.connection.execute("SELECT COUNT(*) AS c FROM sched_versions").fetchone()
        assignments = self.database.connection.execute("SELECT COUNT(*) AS c FROM sched_assignments").fetchone()
        self.assertEqual(0, versions["c"])
        self.assertEqual(0, assignments["c"])

    def test_reviewer_has_no_dispatch_authority(self):
        with self.assertRaises(PermissionDenied):
            self.service.confirm_schedule(request_id="confirm-sh1", actor_id="rv1",
                                          shift_id="sh1", assignments=self._full_plan())


class ShortageAndProposalTest(SchedulingTestBase):
    def setUp(self):
        super().setUp()
        self._confirm_sh1()

    def _report_late(self, participant_id="nur1", request_id="shortage-1"):
        receipt = self.service.report_shortage(
            request_id=request_id, actor_id="op1", shift_id="sh1",
            participant_id=participant_id, kind="late",
            expected_at="2026-10-01T11:30:00Z", note="路上拥堵")
        return receipt.resource_id

    def test_shortage_requires_confirmed_assignment(self):
        with self.assertRaises(ValidationError):
            self.service.report_shortage(request_id="shortage-x", actor_id="op1",
                                         shift_id="sh1", participant_id="nur2", kind="absent")

    def test_shortage_report_is_idempotent(self):
        first = self._report_late()
        again = self.service.report_shortage(
            request_id="shortage-2", actor_id="op1", shift_id="sh1",
            participant_id="nur1", kind="late", expected_at="2026-10-01T11:30:00Z")
        self.assertTrue(again.replayed)
        self.assertEqual(first, again.resource_id)

    def test_late_shortage_generates_augment_proposals_with_impact(self):
        shortage_id = self._report_late()
        result = self.service.generate_proposals(actor_id="op1", shortage_id=shortage_id)
        # nur2、nur4、nur5 都具备陪同资质，应生成三个方案
        self.assertEqual(3, len(result["proposals"]))
        for proposal in result["proposals"]:
            self.assertEqual([{"action": "add", "post_id": "p-acc",
                               "participant_id": proposal["changes"][0]["participant_id"]}],
                             proposal["changes"])
            self.assertTrue(proposal["impact"]["staffing"])
            self.assertTrue(proposal["impact"]["notes"])
        rejected = {row["participant_id"]: row["reasons"] for row in result["rejections"]}
        self.assertIn("qualification_missing", rejected["vol1"])
        self.assertIn("qualification_expired", rejected["nur3"])
        self.assertIn("duplicate_in_plan", rejected["doc1"])

    def test_early_leave_proposal_swaps_participant(self):
        receipt = self.service.report_shortage(
            request_id="shortage-leave", actor_id="op1", shift_id="sh1",
            participant_id="nur1", kind="early_leave", note="身体不适提前离场")
        result = self.service.generate_proposals(actor_id="op1", shortage_id=receipt.resource_id)
        actions = {change["action"] for change in result["proposals"][0]["changes"]}
        self.assertEqual({"remove", "add"}, actions)

    def test_generate_proposals_is_idempotent(self):
        shortage_id = self._report_late()
        first = self.service.generate_proposals(actor_id="op1", shortage_id=shortage_id)
        second = self.service.generate_proposals(actor_id="op1", shortage_id=shortage_id)
        self.assertEqual([p["proposal_id"] for p in first["proposals"]],
                         [p["proposal_id"] for p in second["proposals"]])

    def test_regenerate_proposals_replaces_pending_and_rejections(self):
        shortage_id = self._report_late()
        first = self.service.generate_proposals(actor_id="op1", shortage_id=shortage_id)
        regenerated = self.service.generate_proposals(actor_id="op1", shortage_id=shortage_id,
                                                      regenerate=True)
        self.assertNotEqual([p["proposal_id"] for p in first["proposals"]],
                            [p["proposal_id"] for p in regenerated["proposals"]])
        self.assertEqual(len(first["rejections"]), len(regenerated["rejections"]))
        pending = self.service.list_proposals(actor_id="op1", shortage_id=shortage_id)
        self.assertEqual(len(regenerated["proposals"]), len(pending))

    def test_shortage_requires_confirmed_version(self):
        self.service.register_shift(request_id="shift-9", actor_id="op1", shift_id="sh9",
                                    zone_id="z1", name="未排班班次",
                                    start_at="2026-10-03T10:00:00Z",
                                    end_at="2026-10-03T14:00:00Z")
        with self.assertRaises(ValidationError):
            self.service.report_shortage(request_id="shortage-9", actor_id="op1",
                                         shift_id="sh9", participant_id="nur1", kind="absent")

    def test_confirm_proposal_creates_new_version_and_resolves(self):
        shortage_id = self._report_late()
        proposals = self.service.generate_proposals(actor_id="op1", shortage_id=shortage_id)
        receipt = self.service.confirm_proposal(
            request_id="confirm-proposal", actor_id="op1",
            proposal_id=proposals["proposals"][0]["proposal_id"])
        self.assertFalse(receipt.replayed)
        versions = self._versions()
        self.assertEqual(2, len(versions))
        self.assertEqual("confirmed", versions[-1]["status"])
        self.assertEqual("superseded", versions[0]["status"])
        self.assertEqual(6, versions[-1]["assignments"])
        shortage = self.database.connection.execute(
            "SELECT status FROM sched_shortages WHERE shortage_id=?", (shortage_id,)).fetchone()
        self.assertEqual("resolved", shortage["status"])
        pending = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM sched_proposals WHERE shortage_id=? AND status='pending'",
            (shortage_id,)).fetchone()
        self.assertEqual(0, pending["c"])

    def test_confirm_proposal_revalidates_and_rolls_back(self):
        # log1 提前离场，log4 是替岗候选人
        shortage = self.service.report_shortage(
            request_id="shortage-log", actor_id="op1", shift_id="sh1",
            participant_id="log1", kind="early_leave")
        proposals = self.service.generate_proposals(actor_id="op1", shortage_id=shortage.resource_id)
        proposal_id = proposals["proposals"][0]["proposal_id"]
        self.assertEqual("log4", proposals["proposals"][0]["changes"][-1]["participant_id"])
        # 生成方案后，log4 被另一个重叠班次确认占用，方案确认必须整体失败
        self.service.confirm_schedule(
            request_id="confirm-sh2", actor_id="op1", shift_id="sh2",
            assignments=self._full_plan(**{"p-lead": "lead2", "p-diag": "doc2", "p-acc": "nur5",
                                           "p-sup": "log4", "p-ord": "log5"}))
        with self.assertRaises(PlanRejected) as caught:
            self.service.confirm_proposal(request_id="confirm-proposal", actor_id="op1",
                                          proposal_id=proposal_id)
        self.assertIn("time_conflict", self._reasons(caught.exception))
        # 不留半套变更：版本仍是 1，缺员与方案保持待处理
        self.assertEqual(1, len(self._versions()))
        shortage_row = self.database.connection.execute(
            "SELECT status FROM sched_shortages WHERE shortage_id=?",
            (shortage.resource_id,)).fetchone()
        proposal_row = self.database.connection.execute(
            "SELECT status FROM sched_proposals WHERE proposal_id=?", (proposal_id,)).fetchone()
        self.assertEqual("open", shortage_row["status"])
        self.assertEqual("pending", proposal_row["status"])

    def test_uncovered_dependencies_reflect_open_shortage(self):
        self._report_late()
        during = self.service.uncovered_dependencies(actor_id="op1", shift_id="sh1",
                                                     at="2026-10-01T10:30:00Z")
        self.assertTrue(during["issues"])
        self.assertTrue(during["staffing_gaps"])
        after_arrival = self.service.uncovered_dependencies(actor_id="op1", shift_id="sh1",
                                                            at="2026-10-01T12:00:00Z")
        self.assertEqual([], after_arrival["issues"])

    def test_dispatch_rejections_are_queryable(self):
        shortage_id = self._report_late()
        self.service.generate_proposals(actor_id="op1", shortage_id=shortage_id)
        rejections = self.service.dispatch_rejections(actor_id="op1", shortage_id=shortage_id)
        self.assertTrue(rejections)
        self.assertTrue(all(row["reason_text"] for row in rejections))


class CheckinTest(SchedulingTestBase):
    def setUp(self):
        super().setUp()
        self._confirm_sh1()

    def test_checkin_request_and_natural_idempotency(self):
        first = self.service.record_checkin(request_id="ci-1", actor_id="op1",
                                            shift_id="sh1", participant_id="doc1")
        self.assertFalse(first.replayed)
        replay = self.service.record_checkin(request_id="ci-1", actor_id="op1",
                                             shift_id="sh1", participant_id="doc1")
        self.assertTrue(replay.replayed)
        natural = self.service.record_checkin(request_id="ci-2", actor_id="op1",
                                              shift_id="sh1", participant_id="doc1")
        self.assertTrue(natural.replayed)
        self.assertEqual(first.resource_id, natural.resource_id)
        count = self.database.connection.execute(
            "SELECT COUNT(*) AS c FROM sched_checkins").fetchone()
        self.assertEqual(1, count["c"])

    def test_late_receipt_is_idempotent(self):
        first = self.service.record_checkin(request_id="late-1", actor_id="op1", shift_id="sh1",
                                            participant_id="nur1", kind="late_receipt",
                                            occurred_at="2026-10-01T11:35:00Z")
        again = self.service.record_checkin(request_id="late-2", actor_id="op1", shift_id="sh1",
                                            participant_id="nur1", kind="late_receipt",
                                            occurred_at="2026-10-01T11:40:00Z")
        self.assertTrue(again.replayed)
        self.assertEqual(first.resource_id, again.resource_id)

    def test_checkin_fact_cannot_be_rewritten(self):
        self.service.record_checkin(request_id="ci-1", actor_id="op1",
                                    shift_id="sh1", participant_id="doc1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute(
                "UPDATE sched_checkins SET participant_id='nur1'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.database.connection.execute("DELETE FROM sched_checkins")

    def test_checkin_requires_current_assignment(self):
        with self.assertRaises(ValidationError):
            self.service.record_checkin(request_id="ci-x", actor_id="op1",
                                        shift_id="sh1", participant_id="nur2")


class TakeoverTest(SchedulingTestBase):
    def setUp(self):
        super().setUp()
        self._confirm_sh1()

    def _service_at(self, moment):
        return SchedulingService(self.database, FixedClock(moment))

    def _register_takeover(self, service=None, request_id="takeover-1", **overrides):
        service = service or self.service
        params = dict(request_id=request_id, actor_id="op1", site_id="s1", holder_id="rv1",
                      reason="负责人临时离岗", valid_from="2026-10-01T12:00:00Z",
                      valid_until="2026-10-01T13:00:00Z",
                      handover_items=["对讲机与钥匙", "未完成的替岗确认"])
        params.update(overrides)
        return service.register_takeover(**params)

    def test_takeover_grants_dispatch_only_within_window(self):
        self._register_takeover()
        plan = self._full_plan(**{"p-acc": "nur2"})
        inside = self._service_at(datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc))
        receipt = inside.confirm_schedule(request_id="confirm-by-holder", actor_id="rv1",
                                          shift_id="sh1", assignments=plan)
        self.assertFalse(receipt.replayed)
        outside = self._service_at(datetime(2026, 10, 1, 13, 30, tzinfo=timezone.utc))
        with self.assertRaises(PermissionDenied):
            outside.confirm_schedule(request_id="confirm-after-expiry", actor_id="rv1",
                                     shift_id="sh1", assignments=plan)
        before = self._service_at(datetime(2026, 10, 1, 11, 0, tzinfo=timezone.utc))
        with self.assertRaises(PermissionDenied):
            before.confirm_schedule(request_id="confirm-before-window", actor_id="rv1",
                                    shift_id="sh1", assignments=plan)

    def test_takeover_requires_items_and_future_window(self):
        with self.assertRaises(ValidationError):
            self._register_takeover(handover_items=[])
        with self.assertRaises(ValidationError):
            self._register_takeover(request_id="takeover-2",
                                    valid_from="2026-10-01T07:00:00Z",
                                    valid_until="2026-10-01T08:00:00Z")

    def test_pending_handovers_survive_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "restart.sqlite3"
            database = Database(path)
            foundation = DomainService(database, FixedClock(self.clock_at))
            service = SchedulingService(database, FixedClock(self.clock_at))
            foundation.register_organization(request_id="org", actor_id="bootstrap",
                                             organization_id="o1", name="夜市组委会")
            foundation.register_actor(request_id="admin", actor_id="bootstrap",
                                      new_actor_id="a1", display_name="管理员",
                                      role="admin", organization_id="o1")
            foundation.register_actor(request_id="lead", actor_id="a1",
                                      new_actor_id="op1", display_name="负责人",
                                      role="operator", organization_id="o1")
            foundation.register_actor(request_id="duty", actor_id="a1",
                                      new_actor_id="rv1", display_name="值班调度",
                                      role="reviewer", organization_id="o1")
            foundation.register_site(request_id="site", actor_id="op1", site_id="s1",
                                     organization_id="o1", name="主会场",
                                     timezone_name="Asia/Shanghai")
            service.register_takeover(request_id="takeover", actor_id="op1", site_id="s1",
                                      holder_id="rv1", reason="离岗",
                                      valid_from="2026-10-01T12:00:00Z",
                                      valid_until="2026-10-01T13:00:00Z",
                                      handover_items=["钥匙", "台账"])
            database.close()
            # 服务恢复：重新打开同一数据库，接续尚未完成的交接
            database = Database(path)
            resumed = SchedulingService(database, FixedClock(
                datetime(2026, 10, 1, 12, 30, tzinfo=timezone.utc)))
            pending = resumed.pending_handovers(actor_id="op1", site_id="s1")
            self.assertEqual(2, len(pending))
            first = resumed.complete_handover(request_id="handover-1", actor_id="rv1",
                                              handover_id=pending[0]["handover_id"])
            self.assertFalse(first.replayed)
            replay = resumed.complete_handover(request_id="handover-1", actor_id="rv1",
                                               handover_id=pending[0]["handover_id"])
            self.assertTrue(replay.replayed)
            remaining = resumed.pending_handovers(actor_id="op1", site_id="s1")
            self.assertEqual(1, len(remaining))
            database.close()


class QueryTest(SchedulingTestBase):
    def setUp(self):
        super().setUp()
        self._confirm_sh1()

    def test_zone_responsible_answers_person_and_takeover(self):
        answer = self.service.zone_responsible(actor_id="op1", zone_id="z1",
                                               at="2026-10-01T11:00:00Z")
        self.assertEqual("lead1", answer["responsible"]["participant_id"])
        self.assertIsNone(answer["dispatch_authority"])
        self.service.register_takeover(request_id="takeover", actor_id="op1", site_id="s1",
                                       holder_id="rv1", reason="离岗",
                                       valid_from="2026-10-01T12:00:00Z",
                                       valid_until="2026-10-01T13:00:00Z",
                                       handover_items=["钥匙"])
        answer = self.service.zone_responsible(actor_id="op1", zone_id="z1",
                                               at="2026-10-01T12:30:00Z")
        self.assertEqual("rv1", answer["dispatch_authority"]["holder_id"])
        answer = self.service.zone_responsible(actor_id="op1", zone_id="z1",
                                               at="2026-10-01T13:30:00Z")
        self.assertIsNone(answer["dispatch_authority"])

    def test_zone_responsible_without_shift(self):
        answer = self.service.zone_responsible(actor_id="op1", zone_id="z1",
                                               at="2026-10-02T11:00:00Z")
        self.assertIsNone(answer["shift_id"])
        self.assertIsNone(answer["responsible"])

    def test_roster_filters_fields_by_post_policy(self):
        roster = self.service.post_roster(actor_id="op1", post_id="p-acc", shift_id="sh1")
        self.assertEqual("13800000000", roster["items"][0]["phone"])
        self.assertNotIn("qualifications", roster["items"][0])
        roster = self.service.post_roster(actor_id="op1", post_id="p-ord", shift_id="sh1")
        self.assertIn("name", roster["items"][0])
        self.assertNotIn("phone", roster["items"][0])
        self.assertNotIn("title", roster["items"][0])

    def test_roster_read_permission(self):
        with self.assertRaises(PermissionDenied):
            self.service.post_roster(actor_id="au1", post_id="p-acc", shift_id="sh1")
        roster = self.service.post_roster(actor_id="rv1", post_id="p-acc", shift_id="sh1")
        self.assertEqual(1, len(roster["items"]))

    def test_get_participant_requires_manage_role(self):
        with self.assertRaises(PermissionDenied):
            self.service.get_participant(actor_id="rv1", participant_id="doc1")
        profile = self.service.get_participant(actor_id="op1", participant_id="doc1")
        self.assertEqual("diagnosis", profile["qualifications"][0]["skill"])

    def test_request_id_rejects_changed_payload(self):
        self.service.record_checkin(request_id="ci-1", actor_id="op1",
                                    shift_id="sh1", participant_id="doc1")
        with self.assertRaises(ConflictError):
            self.service.record_checkin(request_id="ci-1", actor_id="op1",
                                        shift_id="sh1", participant_id="nur1")

    def test_audit_chain_stays_valid(self):
        self.service.record_checkin(request_id="ci-1", actor_id="op1",
                                    shift_id="sh1", participant_id="doc1")
        valid, count = self.foundation.verify_audit()
        self.assertTrue(valid)
        self.assertGreater(count, 0)


class RegistryRuleTest(SchedulingTestBase):
    def test_dependency_cycle_is_rejected(self):
        self.service.register_post(request_id="post-a", actor_id="op1", post_id="p-a",
                                   zone_id="z1", name="岗位甲", required_skill="supply",
                                   min_staff=0)
        self.service.register_post(request_id="post-b", actor_id="op1", post_id="p-b",
                                   zone_id="z1", name="岗位乙", required_skill="supply",
                                   min_staff=0)
        self.service.add_post_dependency(request_id="dep-ab", actor_id="op1",
                                         post_id="p-a", depends_on_post_id="p-b")
        with self.assertRaises(ValidationError):
            self.service.add_post_dependency(request_id="dep-ba", actor_id="op1",
                                             post_id="p-b", depends_on_post_id="p-a")

    def test_only_one_responsible_post_per_zone(self):
        with self.assertRaises(ConflictError):
            self.service.register_post(request_id="post-lead-2", actor_id="op1",
                                       post_id="p-lead-2", zone_id="z1", name="第二责任人",
                                       required_skill="coordination", min_staff=1,
                                       is_responsible=True)

    def test_necessary_fields_are_whitelisted(self):
        with self.assertRaises(ValidationError):
            self.service.register_post(request_id="post-bad", actor_id="op1", post_id="p-bad",
                                       zone_id="z1", name="坏岗位", required_skill="supply",
                                       min_staff=0, necessary_fields=["salary"])

    def test_unknown_shift_raises_not_found(self):
        with self.assertRaises(NotFoundError):
            self.service.shift_versions(actor_id="op1", shift_id="missing")


if __name__ == "__main__":
    unittest.main()
