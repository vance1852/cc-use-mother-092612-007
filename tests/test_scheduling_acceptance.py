import unittest

from night_market_scheduling.acceptance import run


class SchedulingAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual(2, result["versions"])
        self.assertGreaterEqual(result["proposals"], 1)
        self.assertGreater(result["rejections"], 0)
        self.assertEqual(1, result["uncovered_before"])
        self.assertEqual(0, result["uncovered_after"])
        self.assertEqual("p-lead", result["responsible"])
        self.assertTrue(result["checkin_replayed"])
        self.assertTrue(result["late_receipt_replayed"])
        self.assertTrue(result["roster_phone_visible"])
        self.assertTrue(result["roster_phone_hidden"])
        self.assertEqual("rv-duty", result["takeover_holder_in_window"])
        self.assertIsNone(result["takeover_holder_after_window"])
        self.assertEqual(2, result["handovers_completed"])
        self.assertEqual(0, result["handovers_remaining"])


if __name__ == "__main__":
    unittest.main()
