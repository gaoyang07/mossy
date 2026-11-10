"""Gesture selection, turn ownership and cancellation without robot hardware."""

import unittest
from unittest.mock import patch

import numpy as np
from scipy.spatial.transform import Rotation

from mossy.expressions import NEUTRAL, POSTURES, Expressions, classify_response


class FakeMove:
    duration = 3.0

    def evaluate(self, t):
        head = np.eye(4)
        head[:3, :3] = Rotation.from_euler("xyz", [0.5, 0.3, 0.2]).as_matrix()
        head[:3, 3] = [0.1, 0.2, -0.1]
        return head, np.array([2.0, -2.0]), 0.4


class PoseOnlyRobot:
    """Intentionally has no media, play_move or cancel_move methods."""

    def __init__(self):
        self.poses = []

    def set_target(self, *, head, antennas):
        self.poses.append((head.copy(), antennas.copy()))


class ClassificationTests(unittest.TestCase):
    def test_explicit_chinese_expressions(self):
        cases = {
            "太好了！恭喜你完成训练。": "happy",
            "恭喜你，实验成功了。": "happy",
            "很高兴认识你。": "happy",
            "好的，我来帮你看看。": "agree",
            "没问题。": "agree",
            "当然可以！": "agree",
            "让我想想。": "thinking",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify_response(text), expected)

    def test_negation_quotes_questions_and_neutral_explanations(self):
        cases = [
            "我不开心。",
            "我很高兴，但是结果并不好。",
            "太好了，不过还有一个问题。",
            "你今天开心吗？",
            "很高兴吗？",
            "他说：太好了！",
            "“太好了”是他说的话。",
            "好的，不能这样操作。",
            "这个配置使用了八张 GPU。",
            "如果成功了，我会很高兴。",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(classify_response(text, final=True))

    def test_wait_for_complete_sentence_and_allow_final_short_answer(self):
        self.assertIsNone(classify_response("太好了"))
        self.assertEqual(classify_response("太好了", final=True), "happy")
        self.assertIsNone(classify_response("好的，但是"))


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.robot = PoseOnlyRobot()
        self.controller = Expressions(
            self.robot,
            moves={kind: FakeMove() for kind in ("happy", "agree", "thinking", "listening")},
            clock=lambda: self.now,
        )
        self.scope = {"run_id": "run-1", "turn_id": "turn-1"}

    def event(self, action, data=None):
        self.controller._handle(action, data or {}, self.now)
        self.controller._tick(self.now)

    def advance(self, seconds):
        self.now += seconds
        self.controller._tick(self.now)

    def start_happy(self, scope=None):
        scope = scope or self.scope
        self.event("output.transcript.updated", {**scope, "text": "太好了！"})
        self.event("playback.started", scope)

    def test_text_waits_for_actual_playback_and_repeated_snapshots_do_not_repeat(self):
        self.event("output.transcript.updated", {**self.scope, "text": "太好了！"})
        self.assertEqual(self.robot.poses, [])
        self.event("playback.started", self.scope)
        self.assertEqual(self.controller.snapshot()["active"], "happy")
        for _ in range(5):
            self.event("output.transcript.updated", {**self.scope, "text": "太好了！恭喜你。"})
            self.event("playback.started", self.scope)
            self.advance(0.1)
        self.advance(3.0)
        self.event("output.transcript.done", {**self.scope, "text": "太好了！恭喜你。"})
        self.assertEqual(self.controller.snapshot()["started"], 1)

    def test_audio_can_arrive_before_text(self):
        self.event("playback.started", self.scope)
        self.assertEqual(self.robot.poses, [])
        self.advance(0.5)
        self.event("output.transcript.done", {**self.scope, "text": "没问题。"})
        self.assertEqual(self.controller.snapshot()["active"], "agree")

    def test_late_text_does_not_start_gesture_after_speech(self):
        self.event("playback.started", self.scope)
        self.advance(1.6)
        self.event("output.transcript.done", {**self.scope, "text": "太好了！"})
        self.assertEqual(self.controller.snapshot()["started"], 0)

    def test_interruption_returns_smoothly_and_never_restarts_cancelled_turn(self):
        self.start_happy()
        self.advance(0.2)
        before = self.robot.poses[-1]
        self.event("response.interrupted", {**self.scope, "text": "太好了！"})
        np.testing.assert_allclose(self.robot.poses[-1][0], before[0])
        self.event("response.audio.stop", self.scope)
        self.event("output.transcript.done", {**self.scope, "text": "太好了！"})
        self.event("playback.started", self.scope)
        self.advance(0.4)
        np.testing.assert_allclose(self.robot.poses[-1][0], NEUTRAL[0], atol=1e-12)
        np.testing.assert_allclose(self.robot.poses[-1][1], NEUTRAL[1], atol=1e-12)
        self.assertEqual(self.controller.snapshot()["started"], 1)
        self.assertEqual(self.controller.snapshot()["cancelled"], 1)
        self.assertIsNone(self.controller.snapshot()["active"])

    def test_cancel_before_first_audio_suppresses_late_audio_start(self):
        self.event("output.transcript.done", {**self.scope, "text": "太好了！"})
        self.event("response.audio.stop", self.scope)
        self.event("playback.started", self.scope)
        self.assertEqual(self.robot.poses, [])

    def test_stale_stop_does_not_cancel_new_turn(self):
        self.start_happy()
        self.event("response.audio.stop", self.scope)
        self.advance(5.0)
        newer = {"run_id": "run-2", "turn_id": "turn-2"}
        self.start_happy(newer)
        self.event("response.audio.stop", self.scope)
        self.assertEqual(self.controller.snapshot()["active"], "happy")
        self.assertEqual(self.controller._motion.scope, ("run-2", "turn-2"))

    def test_server_completed_waits_for_local_playback_completion(self):
        self.start_happy()
        self.event("response.ended", {**self.scope, "status": "completed"})
        self.assertEqual(self.controller.snapshot()["active"], "happy")
        self.event("playback.completed", self.scope)
        self.assertEqual(self.controller.snapshot()["active"], "returning")
        self.advance(0.4)
        self.assertIsNone(self.controller.snapshot()["active"])

    def test_failed_turn_cancels_motion(self):
        self.start_happy()
        self.event("response.ended", {**self.scope, "status": "failed"})
        self.assertEqual(self.controller.snapshot()["active"], "returning")

    def test_full_duplex_backchannel_does_not_steal_response_motion(self):
        self.start_happy()
        self.event("input.transcript.updated", {"text": "嗯"})
        self.assertEqual(self.controller.snapshot()["active"], "happy")
        self.assertEqual(self.controller.snapshot()["started"], 1)

    def test_listening_and_delayed_thinking_yield_to_audio(self):
        self.event("input.transcript.updated", {"text": "帮我想一下"})
        self.assertEqual(self.controller.snapshot()["active"], "listening")
        self.event("input.transcript.done", {"text": "帮我想一下"})
        self.advance(1.7)
        self.advance(0.1)
        self.assertEqual(self.controller.snapshot()["active"], "thinking")
        self.event("playback.started", self.scope)
        self.assertEqual(self.controller.snapshot()["active"], "returning")

    def test_disable_and_disconnect_cancel_without_media_access(self):
        self.start_happy()
        self.event("motion.enabled", {"enabled": False})
        self.advance(0.4)
        self.assertIsNone(self.controller.snapshot()["active"])
        self.event("input.transcript.updated", {"text": "你好"})
        self.assertEqual(self.controller.snapshot()["started"], 1)
        self.event("motion.enabled", {"enabled": True})
        self.advance(5.0)
        self.start_happy()
        self.event("session.disconnected")
        self.advance(0.4)
        self.assertIsNone(self.controller.snapshot()["active"])

    def test_response_cooldown_drops_gesture_instead_of_backlogging(self):
        self.start_happy()
        self.event("playback.completed", self.scope)
        self.advance(0.4)
        newer = {"run_id": "run-2", "turn_id": "turn-2"}
        self.start_happy(newer)
        self.advance(5.0)
        self.assertEqual(self.controller.snapshot()["started"], 1)

    def test_pose_bounds_and_no_body_rotation_commands(self):
        self.start_happy()
        self.advance(0.4)
        head, antennas = self.robot.poses[-1]
        angle = Rotation.from_matrix(head[:3, :3]).magnitude()
        self.assertLessEqual(angle, np.deg2rad(10) + 1e-12)
        self.assertTrue(np.all(np.abs(head[:3, 3]) <= 0.01))
        self.assertTrue(np.all(np.abs(antennas - NEUTRAL[1]) <= 0.65 + 1e-12))

    def test_resting_posture_changes_smoothly_and_gestures_return_to_it(self):
        rest_head, rest_antennas = POSTURES["relaxed"]
        self.controller.set_posture("relaxed")
        self.controller._handle(*self.controller._events.popleft()[:2], self.now)
        self.controller._tick(self.now)
        self.assertEqual(self.controller.snapshot()["posture"], "relaxed")
        np.testing.assert_allclose(self.robot.poses[-1][0], NEUTRAL[0], atol=1e-12)
        self.advance(0.4)
        np.testing.assert_allclose(self.robot.poses[-1][0], rest_head, atol=1e-12)
        np.testing.assert_allclose(self.robot.poses[-1][1], rest_antennas, atol=1e-12)

        self.start_happy()
        self.advance(0.4)
        head, antennas = self.robot.poses[-1]
        offset = Rotation.from_matrix(rest_head[:3, :3].T @ head[:3, :3]).magnitude()
        self.assertGreater(offset, np.deg2rad(1))
        self.assertLessEqual(offset, np.deg2rad(10) + 1e-12)
        self.assertTrue(np.all(np.abs(antennas - rest_antennas) <= 0.65 + 1e-12))
        self.event("playback.completed", self.scope)
        self.advance(0.4)
        np.testing.assert_allclose(self.robot.poses[-1][0], rest_head, atol=1e-12)
        np.testing.assert_allclose(self.robot.poses[-1][1], rest_antennas, atol=1e-12)

    def test_changing_posture_cancels_a_gesture_in_progress(self):
        self.start_happy()
        self.advance(0.2)
        self.event("motion.posture", {"name": "curious"})
        self.assertEqual(self.controller.snapshot()["active"], "returning")
        self.advance(0.4)
        np.testing.assert_allclose(self.robot.poses[-1][0], POSTURES["curious"][0], atol=1e-12)
        self.assertIsNone(self.controller.snapshot()["active"])

    def test_every_posture_stays_near_the_sdk_pose(self):
        from mossy import config

        self.assertEqual(tuple(POSTURES), config.POSTURES)
        for name, (head, antennas) in POSTURES.items():
            with self.subTest(posture=name):
                angle = Rotation.from_matrix(head[:3, :3]).magnitude()
                self.assertLessEqual(angle, np.deg2rad(8.6))
                self.assertTrue(np.all(np.abs(head[:3, 3]) <= 0.004 + 1e-12))
                self.assertTrue(np.all(np.abs(antennas - NEUTRAL[1]) <= 0.35 + 1e-12))
                # Antennas are (right, left) and never cross over the head.
                self.assertLess(antennas[0], 0)
                self.assertGreater(antennas[1], 0)

    def test_preview_plays_one_gesture_outside_a_reply_unless_gestures_are_off(self):
        self.event("motion.preview", {"kind": "agree"})
        self.assertEqual(self.controller.snapshot()["active"], "agree")
        self.advance(2.0)
        self.advance(0.4)
        self.assertIsNone(self.controller.snapshot()["active"])
        np.testing.assert_allclose(self.robot.poses[-1][0], NEUTRAL[0], atol=1e-12)
        self.event("motion.enabled", {"enabled": False})
        self.event("motion.preview", {"kind": "agree"})
        self.assertEqual(self.controller.snapshot()["started"], 1)

    def test_intensity_applies_to_the_next_gesture(self):
        self.event("motion.intensity", {"value": 0.0})
        self.start_happy()
        self.advance(0.4)
        np.testing.assert_allclose(self.robot.poses[-1][0], NEUTRAL[0], atol=1e-12)
        np.testing.assert_allclose(self.robot.poses[-1][1], NEUTRAL[1], atol=1e-12)

    def test_invalid_requests_are_rejected_before_they_reach_the_worker(self):
        for request, argument in [
            (self.controller.set_posture, "handstand"),
            (self.controller.set_intensity, 1.5),
            (self.controller.preview, "moonwalk"),
        ]:
            with self.subTest(argument=argument):
                with self.assertRaises(ValueError):
                    request(argument)
        self.assertFalse(self.controller._events)

    def test_missing_cached_assets_disable_only_expressions(self):
        controller = Expressions(self.robot)
        with patch("mossy.expressions.load_moves", side_effect=FileNotFoundError("cache missing")):
            with self.assertLogs("mossy.expressions", level="ERROR"):
                controller.start()
                controller._thread.join(timeout=2)
        self.assertFalse(controller.snapshot()["available"])
        self.assertIn("cache missing", controller.snapshot()["error"])
        self.assertEqual(self.robot.poses, [])


if __name__ == "__main__":
    unittest.main()
