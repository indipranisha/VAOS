"""Pure object-rule evaluation tests; no VAOS service or camera is loaded."""

import unittest

from tracking_rules import evaluate_rules, validate_rule


def make_rule(**updates):
    payload = {
        "name": "Watch the entrance",
        "enabled": True,
        "rule_type": "count_in_region",
        "class_name": "person",
        "min_confidence": 0.35,
        "threshold_count": 2,
        "min_consecutive_frames": 2,
        "cooldown_seconds": 10,
        "region": {"x": 0, "y": 0, "width": 1, "height": 1},
    }
    payload.update(updates)
    rule = validate_rule(payload)
    rule["id"] = 7
    return rule


class TrackingRuleTests(unittest.TestCase):
    def setUp(self):
        self.state = {}
        self.people = [
            {"label": "person", "score": 0.8, "box": [10, 10, 30, 50]},
            {"label": "person", "score": 0.7, "box": [40, 10, 60, 50]},
        ]

    def test_count_needs_consecutive_frames_and_obeys_cooldown(self):
        rule = make_rule()
        args = {"rules": [rule], "camera_token": "camera-token-0001", "objects": self.people,
                "frame_size": (100, 100), "state": self.state, "tracking_enabled": True}
        self.assertEqual(evaluate_rules(**args, now=1), [])
        first = evaluate_rules(**args, now=2)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["object_count"], 2)
        self.assertEqual(evaluate_rules(**args, now=5), [])
        self.assertEqual(len(evaluate_rules(**args, now=12)), 1)

    def test_region_and_camera_scope_limit_count(self):
        rule = make_rule(camera_token="camera-token-0001", threshold_count=1,
                         min_consecutive_frames=1, region={"x": 0.7, "y": 0, "width": 0.3, "height": 1})
        args = {"rules": [rule], "camera_token": "camera-token-0002", "objects": self.people,
                "frame_size": (100, 100), "state": self.state, "now": 1, "tracking_enabled": True}
        self.assertEqual(evaluate_rules(**args), [])
        args["camera_token"] = "camera-token-0001"
        self.assertEqual(evaluate_rules(**args), [])

    def test_invalid_geometry_and_threshold_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "fit inside"):
            validate_rule({"name": "bad", "rule_type": "count_in_region", "class_name": "person",
                           "region": {"x": 0.9, "y": 0, "width": 0.2, "height": 1}})
        with self.assertRaisesRegex(ValueError, "Object count"):
            validate_rule({"name": "bad", "rule_type": "count_in_region", "class_name": "person", "threshold_count": 0})

    def test_dwell_needs_track_continuity_and_tracking_enabled(self):
        rule = make_rule(rule_type="dwell_in_region", threshold_count=1, min_consecutive_frames=2, dwell_seconds=3)
        tracked = [{**self.people[0], "_tracking_key": "track-a"}]
        args = {"rules": [rule], "camera_token": "camera-token-0001", "objects": tracked,
                "frame_size": (100, 100), "state": self.state, "tracking_enabled": True}
        self.assertEqual(evaluate_rules(**args, now=1), [])
        self.assertEqual(evaluate_rules(**args, now=3), [])
        self.assertEqual(len(evaluate_rules(**args, now=4)), 1)
        args["tracking_enabled"] = False
        self.assertEqual(evaluate_rules(**args, now=20), [])
        args["tracking_enabled"] = True
        args["objects"] = []
        evaluate_rules(**args, now=21)
        args["objects"] = tracked
        self.assertEqual(evaluate_rules(**args, now=30), [])

    def test_disabled_rule_emits_nothing(self):
        rule = make_rule(enabled=False, threshold_count=1, min_consecutive_frames=1)
        self.assertEqual(evaluate_rules(rules=[rule], camera_token="camera-token-0001", objects=self.people,
                                        frame_size=(100, 100), state=self.state, now=1, tracking_enabled=True), [])


if __name__ == "__main__":
    unittest.main()
