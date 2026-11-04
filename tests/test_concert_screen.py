"""Checks that weak or invalid model evidence cannot admit videos."""
import unittest
from stagesync.intelligence.concert_screen import validate_result


class TestConcertScreen(unittest.TestCase):
    def test_accept_requires_both_modalities_and_high_confidence(self):
        good = dict(decision="accept", confidence=0.97, reason="Live performance",
                    visual_live_performance=True, audio_live_performance=True)
        self.assertEqual(validate_result(good)["decision"], "accept")
        for change in ({"confidence": 0.8}, {"visual_live_performance": False},
                       {"audio_live_performance": False}, {"confidence": float("nan")},
                       {"confidence": "0.99"}, {"decision": "unknown"}):
            self.assertEqual(validate_result({**good, **change})["decision"], "review")
