import unittest

from night_market_foundation.api import route
from night_market_foundation.scheduling import SchedulingService
from night_market_foundation.storage import Database


class SchedulingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = SchedulingService(self.database)
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "组委会"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "lead", "new_actor_id": "lead", "display_name": "负责人",
               "role": "operator", "organization_id": "o1"},
              {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/actors",
              {"request_id": "coord", "new_actor_id": "coord", "display_name": "协调员",
               "role": "reviewer", "organization_id": "o1"},
              {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/actors",
              {"request_id": "auditor", "new_actor_id": "au1", "display_name": "审计员",
               "role": "auditor", "organization_id": "o1"},
              {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/sites",
              {"request_id": "site", "site_id": "s1", "organization_id": "o1",
               "name": "主会场", "timezone_name": "Asia/Shanghai"},
              {"X-Actor-Id": "lead"})
        route(self.service, "POST", "/scheduling/zones",
              {"request_id": "zone", "site_id": "s1", "zone_id": "z1", "name": "义诊专区"},
              {"X-Actor-Id": "lead"})
        route(self.service, "POST", "/scheduling/shifts",
              {"request_id": "shift", "shift_id": "sh1", "zone_id": "z1",
               "start_ts": "2026-09-27T18:00:00+08:00", "end_ts": "2026-09-27T21:00:00+08:00",
               "positions": [
                   {"position_id": "pos-consult", "title": "名中医坐诊", "skill": "consultation",
                    "min_staff": 1, "responsible": True},
                   {"position_id": "pos-order", "title": "秩序维护", "skill": "order",
                    "min_staff": 1},
               ]},
              {"X-Actor-Id": "lead"})
        route(self.service, "POST", "/scheduling/participants",
              {"request_id": "p1", "site_id": "s1", "participant_id": "p-wang",
               "name": "王医师", "role_type": "doctor", "contact": {"phone": "13800000001"}},
              {"X-Actor-Id": "lead"})
        route(self.service, "POST", "/scheduling/qualifications",
              {"request_id": "q1", "participant_id": "p-wang", "qualification_id": "q-wang",
               "skill": "consultation", "certificate_no": "ZY-001",
               "valid_from": "2026-09-01T00:00:00+08:00",
               "valid_until": "2026-10-31T23:59:00+08:00"},
              {"X-Actor-Id": "lead"})
        route(self.service, "POST", "/scheduling/availability",
              {"request_id": "av1", "participant_id": "p-wang",
               "windows": [{"start": "2026-09-27T17:00:00+08:00",
                            "end": "2026-09-27T22:00:00+08:00"}]},
              {"X-Actor-Id": "lead"})
        route(self.service, "POST", "/scheduling/assignments",
              {"request_id": "as1", "shift_id": "sh1", "position_id": "pos-consult",
               "participant_id": "p-wang"},
              {"X-Actor-Id": "lead"})

    def tearDown(self):
        self.database.close()

    def test_participant_write_replays_over_http(self):
        body = {"request_id": "p2", "site_id": "s1", "participant_id": "p-li",
                "name": "李护士", "role_type": "nurse"}
        status, payload = route(self.service, "POST", "/scheduling/participants", body,
                                {"X-Actor-Id": "lead"})
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = route(self.service, "POST", "/scheduling/participants", body,
                                {"X-Actor-Id": "lead"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_shift_detail_and_revision_routes(self):
        status, payload = route(self.service, "GET", "/scheduling/shifts?shift_id=sh1", None,
                                {"X-Actor-Id": "lead"})
        self.assertEqual(200, status)
        self.assertEqual(2, payload["version"])
        self.assertEqual([1, 2], payload["versions"])
        status, payload = route(self.service, "GET",
                                "/scheduling/shifts/revision?shift_id=sh1&version=2", None,
                                {"X-Actor-Id": "lead"})
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["assignments"]))

    def test_responsible_route(self):
        status, payload = route(
            self.service, "GET",
            "/scheduling/zones/responsible?zone_id=z1&at=2026-09-27T19:00:00%2B08:00",
            None, {"X-Actor-Id": "lead"})
        self.assertEqual(200, status)
        self.assertEqual("sh1", payload["shift_id"])
        self.assertEqual("p-wang", payload["responsible"][0]["participant_id"])
        status, payload = route(self.service, "GET", "/scheduling/zones/responsible?zone_id=z1",
                                None, {"X-Actor-Id": "lead"})
        self.assertEqual(400, status)

    def test_uncovered_dependencies_route(self):
        status, payload = route(self.service, "GET",
                                "/scheduling/shifts/uncovered-dependencies?shift_id=sh1",
                                None, {"X-Actor-Id": "lead"})
        self.assertEqual(200, status)
        self.assertEqual([], payload["uncovered"])

    def test_participants_view_permission_over_http(self):
        status, _ = route(self.service, "GET", "/scheduling/participants?shift_id=sh1&view=order",
                          None, {"X-Actor-Id": "au1"})
        self.assertEqual(403, status)
        status, _ = route(self.service, "GET", "/scheduling/participants?shift_id=sh1",
                          None, {"X-Actor-Id": "coord"})
        self.assertEqual(400, status)
        status, payload = route(self.service, "GET",
                                "/scheduling/participants?shift_id=sh1&view=order",
                                None, {"X-Actor-Id": "coord"})
        self.assertEqual(200, status)
        self.assertEqual({"participant_id", "name", "role_type"}, set(payload["items"][0]))

    def test_checkin_route_validation_and_idempotency(self):
        body = {"request_id": "c1", "shift_id": "sh1", "participant_id": "p-wang",
                "kind": "checkin", "occurred_at": "2026-09-27T18:05:00+08:00"}
        status, payload = route(self.service, "POST", "/scheduling/checkins", body,
                                {"X-Actor-Id": "lead"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "POST", "/scheduling/checkins",
                                {**body, "request_id": "c2"}, {"X-Actor-Id": "lead"})
        self.assertEqual(201, status)
        self.assertEqual(payload["resource_id"],
                         route(self.service, "GET", "/scheduling/checkins?shift_id=sh1",
                               None, {"X-Actor-Id": "lead"})[1]["items"][0]["checkin_id"])
        bad = {**body, "request_id": "c3", "kind": "undo"}
        status, payload = route(self.service, "POST", "/scheduling/checkins", bad,
                                {"X-Actor-Id": "lead"})
        self.assertEqual(400, status)

    def test_dispatch_logs_route(self):
        body = {"request_id": "bad", "shift_id": "sh1", "position_id": "pos-order",
                "participant_id": "p-wang"}
        status, payload = route(self.service, "POST", "/scheduling/assignments", body,
                                {"X-Actor-Id": "lead"})
        self.assertEqual(409, status)
        status, payload = route(self.service, "GET",
                                "/scheduling/dispatch-logs?shift_id=sh1&result=rejected",
                                None, {"X-Actor-Id": "lead"})
        self.assertEqual(200, status)
        codes = [reason["code"] for log in payload["items"] for reason in log["reasons"]]
        self.assertIn("QUALIFICATION_MISSING", codes)

    def test_unknown_scheduling_route_returns_404(self):
        status, payload = route(self.service, "GET", "/scheduling/unknown", None,
                                {"X-Actor-Id": "lead"})
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
