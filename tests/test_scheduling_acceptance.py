import unittest

from night_market_foundation.scheduling_acceptance import run


class SchedulingAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual(["p-wang"], result["responsible"])
        self.assertEqual(0, result["uncovered_dependencies"])
        self.assertEqual(2, result["checkin_rows"])
        self.assertTrue(result["checkin_deduplicated"])
        self.assertGreaterEqual(result["plan_options"], 1)
        self.assertTrue(result["rejection_logged"])
        self.assertTrue(result["takeover_expired"])
        self.assertTrue(result["order_view_least_privilege"])
        self.assertTrue(result["escort_view_has_phone"])
        self.assertTrue(result["auditor_blocked"])
        self.assertTrue(result["handovers_resumed"])
        self.assertTrue(result["handovers_done"])


if __name__ == "__main__":
    unittest.main()
