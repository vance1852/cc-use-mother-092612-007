import unittest
from datetime import datetime, timezone

from night_market_foundation.clock import FixedClock
from night_market_foundation.service import DomainService
from night_market_foundation.storage import Database
from night_market_scheduling.api import combined_route, route
from night_market_scheduling.service import SchedulingService


class SchedulingApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        clock = FixedClock(datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc))
        self.foundation = DomainService(self.database, clock)
        self.scheduling = SchedulingService(self.database, clock)
        self.dispatch = combined_route(self.scheduling, self.foundation)
        self.foundation.register_organization(request_id="org", actor_id="bootstrap",
                                              organization_id="o1", name="夜市组委会")
        self.foundation.register_actor(request_id="lead", actor_id="bootstrap",
                                       new_actor_id="op1", display_name="负责人",
                                       role="operator", organization_id="o1")
        self.foundation.register_site(request_id="site", actor_id="op1", site_id="s1",
                                      organization_id="o1", name="主会场",
                                      timezone_name="Asia/Shanghai")
        self.headers = {"X-Actor-Id": "op1"}

    def tearDown(self):
        self.database.close()

    def test_non_scheduling_path_returns_none(self):
        self.assertIsNone(route(self.scheduling, "GET", "/health", None, self.headers))

    def test_combined_route_delegates_to_foundation(self):
        status, payload = self.dispatch("GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_register_participant_and_replay(self):
        body = {"request_id": "p1", "site_id": "s1", "participant_id": "doc1",
                "name": "林医师", "role_type": "famous_doctor"}
        status, payload = self.dispatch("POST", "/scheduling/participants", body, self.headers)
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = self.dispatch("POST", "/scheduling/participants", body, self.headers)
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_plan_rejected_returns_structured_issues(self):
        self.dispatch("POST", "/scheduling/zones",
                      {"request_id": "z1", "zone_id": "z1", "site_id": "s1", "name": "义诊专区"},
                      self.headers)
        self.dispatch("POST", "/scheduling/posts",
                      {"request_id": "post1", "post_id": "p1", "zone_id": "z1",
                       "name": "物资补给", "required_skill": "supply", "min_staff": 1},
                      self.headers)
        self.dispatch("POST", "/scheduling/shifts",
                      {"request_id": "sh1", "shift_id": "sh1", "zone_id": "z1", "name": "晚班",
                       "start_at": "2026-10-01T10:00:00Z", "end_at": "2026-10-01T14:00:00Z"},
                      self.headers)
        status, payload = self.dispatch("POST", "/scheduling/shifts/sh1/confirm",
                                        {"request_id": "c1", "assignments": []}, self.headers)
        self.assertEqual(400, status)
        status, payload = self.dispatch(
            "POST", "/scheduling/shifts/sh1/confirm",
            {"request_id": "c1", "assignments": [{"post_id": "p1", "participant_id": "ghost"}]},
            self.headers)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_zone_responsible_requires_at(self):
        self.dispatch("POST", "/scheduling/zones",
                      {"request_id": "z1", "zone_id": "z1", "site_id": "s1", "name": "义诊专区"},
                      self.headers)
        status, payload = self.dispatch("GET", "/scheduling/zones/z1/responsible", None,
                                        self.headers)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])
        status, payload = self.dispatch("GET", "/scheduling/zones/z1/responsible?at=2026-10-01T11:00:00Z",
                                        None, self.headers)
        self.assertEqual(200, status)
        self.assertIsNone(payload["responsible"])

    def test_unknown_scheduling_route_returns_404(self):
        status, payload = self.dispatch("GET", "/scheduling/missing", None, self.headers)
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_missing_actor_is_rejected(self):
        status, payload = self.dispatch("POST", "/scheduling/participants",
                                        {"request_id": "p1", "site_id": "s1",
                                         "participant_id": "doc1", "name": "林医师",
                                         "role_type": "famous_doctor"}, None)
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
