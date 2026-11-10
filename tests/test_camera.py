"""Robot-frame encoding and camera lifecycle, without a camera or daemon."""

import io
import unittest
from unittest.mock import Mock

import numpy as np
from PIL import Image

from mossy.camera import Camera, encode_frame, local_camera_access


class FrameEncodingTests(unittest.TestCase):
    def test_bgr_color_order_and_aspect_ratio(self):
        frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        frame[:, :960, 2] = 255  # BGR red.
        frame[:, 960:, 0] = 255  # BGR blue.
        before = frame.copy()
        jpeg = encode_frame(frame, 640, 80)
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (640, 360))
            red, blue = image.getpixel((100, 100)), image.getpixel((540, 100))
            self.assertGreater(red[0], 240)
            self.assertLess(red[2], 15)
            self.assertGreater(blue[2], 240)
            self.assertLess(blue[0], 15)
        np.testing.assert_array_equal(frame, before)

    def test_small_frames_are_not_upscaled(self):
        jpeg = encode_frame(np.zeros((80, 120, 3), dtype=np.uint8), 640, 80)
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.size, (120, 80))

    def test_invalid_sdk_frames_are_rejected(self):
        for frame in [
            np.zeros((8, 8), dtype=np.uint8),
            np.zeros((8, 8, 4), dtype=np.uint8),
            np.zeros((8, 8, 3), dtype=np.float32),
            np.zeros((0, 8, 3), dtype=np.uint8),
        ]:
            with self.subTest(shape=frame.shape, dtype=frame.dtype):
                with self.assertRaises(ValueError):
                    encode_frame(frame, 640, 80)


class CameraLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.capture = Mock(return_value=np.zeros((8, 8, 3), dtype=np.uint8))
        self.send = Mock()
        self.source = Mock()
        self.camera = Camera(
            self.capture, self.send, self.source, enabled=True, clock=lambda: self.now
        )

    def test_capture_submits_jpeg_and_exposes_only_fresh_preview(self):
        self.camera._capture_once()
        self.send.assert_called_once()
        jpeg, captured_at = self.send.call_args.args
        self.assertEqual(captured_at, 100.0)
        self.assertEqual(self.camera.preview(), jpeg)
        self.assertTrue(self.camera.snapshot()["available"])
        self.now += 3.1
        self.assertIsNone(self.camera.preview())
        self.assertFalse(self.camera.snapshot()["available"])

    def test_disabled_camera_does_not_read_or_send_and_clears_preview(self):
        self.camera._capture_once()
        self.capture.reset_mock()
        self.send.reset_mock()
        self.camera.set_enabled(False)
        self.camera._capture_once()
        self.source.assert_called_once_with(False)
        self.capture.assert_not_called()
        self.send.assert_not_called()
        self.assertIsNone(self.camera.preview())
        self.assertFalse(self.camera.snapshot()["available"])

    def test_disabling_during_capture_discards_the_in_flight_frame(self):
        def get_frame():
            self.camera.set_enabled(False)
            return np.zeros((8, 8, 3), dtype=np.uint8)

        self.capture.side_effect = get_frame
        self.camera._capture_once()
        self.send.assert_not_called()
        self.assertIsNone(self.camera.preview())

    def test_missing_frames_clear_preview_and_recover_on_new_frames(self):
        self.camera._capture_once()
        self.capture.return_value = None
        with self.assertLogs("mossy.camera", level="WARNING"):
            self.camera._capture_once()
        self.assertIsNone(self.camera.preview())
        self.assertIn("No Reachy camera frames", self.camera.snapshot()["error"])
        self.assertEqual(self.send.call_count, 1)
        self.capture.return_value = np.zeros((8, 8, 3), dtype=np.uint8)
        self.camera._capture_once()
        self.assertIsNone(self.camera.snapshot()["error"])
        self.assertTrue(self.camera.snapshot()["available"])
        self.assertEqual(self.send.call_count, 2)

    def test_camera_exception_is_contained(self):
        self.capture.side_effect = RuntimeError("camera access denied")
        with self.assertLogs("mossy.camera", level="WARNING"):
            self.camera._capture_once()
        self.assertEqual(self.camera.snapshot()["error"], "camera access denied")
        self.send.assert_not_called()

    def test_stop_disables_input_and_clears_preview(self):
        self.camera._capture_once()
        self.camera.stop()
        self.camera._capture_once()
        self.assertEqual(self.send.call_count, 1)
        self.source.assert_called_once_with(False)
        self.assertIsNone(self.camera.preview())

    def test_capture_settings_have_bounded_ranges(self):
        for kwargs in [{"fps": 0}, {"fps": 10}, {"max_edge": 4096}, {"quality": 100}]:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    Camera(self.capture, self.send, self.source, **kwargs)


class CameraAccessTests(unittest.TestCase):
    def test_local_browser_and_local_cli_are_allowed(self):
        self.assertTrue(
            local_camera_access(
                "127.0.0.1", "127.0.0.1:7863", "http://127.0.0.1:7863", "same-origin"
            )
        )
        self.assertTrue(local_camera_access("::1", "localhost:7863", None, None))

    def test_remote_cross_site_and_rebinding_requests_are_rejected(self):
        for args in [
            ("192.168.1.10", "127.0.0.1:7863", None, None),
            (None, "127.0.0.1:7863", None, None),
            ("127.0.0.1", "attacker.example:7863", None, None),
            ("127.0.0.1", "127.0.0.1:7863", "https://attacker.example", "cross-site"),
            ("127.0.0.1", "127.0.0.1:7863", None, "cross-site"),
            ("127.0.0.1", "127.0.0.1:7863", "null", None),
        ]:
            with self.subTest(args=args):
                self.assertFalse(local_camera_access(*args))


if __name__ == "__main__":
    unittest.main()
