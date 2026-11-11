"""Echo configuration and actual GStreamer flow, without robot hardware."""

import importlib.util
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from mossy.echo import EchoCancellation, enable_software_aec


class HardwareConfigurationTests(unittest.TestCase):
    def test_hardware_is_verified_even_when_software_is_disabled(self):
        audio = Mock()
        audio.apply_audio_config.return_value = True
        echo = EchoCancellation(audio, software=False)
        values = dict(audio.apply_audio_config.call_args.args[0])
        self.assertEqual(values["SHF_BYPASS"], (0,))  # 1 bypasses processing.
        self.assertEqual(values["PP_ECHOONOFF"], (1,))
        self.assertEqual(values["PP_NLATTENONOFF"], (1,))
        self.assertEqual(values["PP_NLAEC_MODE"], (0,))  # Normal, not training mode.
        self.assertTrue(audio.apply_audio_config.call_args.kwargs["verify"])
        self.assertTrue(echo.snapshot()["hardware"]["verified_at_startup"])
        self.assertFalse(echo.snapshot()["software"]["enabled"])

    def test_a_failed_hardware_write_is_not_reported_as_enabled(self):
        audio = Mock()
        audio.apply_audio_config.return_value = False
        with self.assertLogs("mossy.echo", level="WARNING"):
            echo = EchoCancellation(audio, software=False)
        self.assertFalse(echo.snapshot()["hardware"]["verified_at_startup"])
        self.assertIsNone(echo.snapshot()["hardware"]["parameters"])


@unittest.skipUnless(importlib.util.find_spec("gi"), "needs GStreamer Python bindings")
class GStreamerEchoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import gi

            gi.require_version("Gst", "1.0")
            gi.require_version("GstApp", "1.0")
            from gi.repository import Gst, GstApp  # noqa: F401 - registers AppSink methods
        except (ImportError, ValueError) as exc:
            raise unittest.SkipTest("needs GStreamer and GstApp typelibs") from exc

        Gst.init(None)
        for factory in ("webrtcdsp", "webrtcechoprobe", "audiotestsrc", "audiomixer"):
            if Gst.ElementFactory.find(factory) is None:
                raise unittest.SkipTest(f"needs the GStreamer {factory} plugin")
        cls.Gst = Gst

    def setUp(self):
        self.pipeline = self.Gst.parse_launch(
            "audiotestsrc is-live=true freq=997 ! queue ! audioconvert ! audioresample "
            '! appsink name=capture caps="audio/x-raw,format=F32LE,rate=16000,'
            'channels=2,layout=interleaved" '
            'appsrc name=speech is-live=true format=time caps="audio/x-raw,'
            'format=F32LE,rate=16000,channels=2,layout=interleaved" ! queue ! mixer. '
            "audiotestsrc is-live=true wave=silence "
            "! audio/x-raw,format=F32LE,rate=16000,channels=2 ! queue ! mixer. "
            "audiomixer name=mixer ! fakesink sync=true"
        )
        self.audio = SimpleNamespace(
            _pipeline=self.pipeline,
            _appsink_audio=self.pipeline.get_by_name("capture"),
            CHANNELS=2,
            apply_audio_config=Mock(return_value=True),
        )
        self.addCleanup(self.pipeline.set_state, self.Gst.State.NULL)

    def sample(self):
        sample = self.audio._appsink_audio.try_pull_sample(2 * self.Gst.SECOND)
        error = self.pipeline.get_bus().pop_filtered(self.Gst.MessageType.ERROR)
        self.assertIsNone(error, str(error.parse_error()) if error else "")
        self.assertIsNotNone(sample, "capture stopped producing audio")
        buffer = sample.get_buffer()
        values = np.frombuffer(buffer.extract_dup(0, buffer.get_size()), dtype="<f4")
        return sample, values

    def test_real_audio_flows_with_matched_reference_and_no_duplicate_aec(self):
        echo = EchoCancellation(self.audio)
        self.assertEqual(enable_software_aec(self.audio), (echo.dsp, echo.probe))
        self.pipeline.set_state(self.Gst.State.PLAYING)
        sample, values = self.sample()
        self.assertEqual(sample.get_caps().get_structure(0).get_value("format"), "F32LE")
        # A near-end tone with a silent far-end must remain audible.
        self.assertGreater(float(np.sqrt(np.mean(values * values))), 0.05)
        for element in (echo.dsp, echo.probe):
            caps = element.get_static_pad("sink").get_current_caps().get_structure(0)
            self.assertEqual(caps.get_value("rate"), 16000)
            self.assertEqual(caps.get_value("format"), "S16LE")
            self.assertEqual(element.get_parent(), self.pipeline)
        snapshot = echo.snapshot()["software"]
        self.assertTrue(snapshot["properties"]["echo-cancel"])
        for key in ("extended-filter", "delay-agnostic"):
            if key in snapshot["properties"]:
                self.assertTrue(snapshot["properties"][key])
            else:
                self.assertIn(key, snapshot["unsupported_options"])

    def test_a_running_pipeline_is_rejected_without_interrupting_capture(self):
        self.pipeline.set_state(self.Gst.State.PLAYING)
        self.sample()
        with self.assertRaisesRegex(RuntimeError, "before the audio pipeline starts"):
            enable_software_aec(self.audio)
        self.sample()

    def test_echo_probe_receives_the_speaker_signal(self):
        _, probe = enable_software_aec(self.audio)
        reference_peaks = []

        def observe_reference(pad, info):
            buffer = info.get_buffer()
            samples = np.frombuffer(buffer.extract_dup(0, buffer.get_size()), dtype="<i2").astype(
                np.float32
            )
            reference_peaks.append(float(np.max(np.abs(samples))) / 32768)
            return self.Gst.PadProbeReturn.OK

        probe.get_static_pad("src").add_probe(self.Gst.PadProbeType.BUFFER, observe_reference)
        self.pipeline.set_state(self.Gst.State.PLAYING)
        self.sample()
        # A nonzero reference catches a probe accidentally attached to the
        # silence generator instead of the mixed speaker path.
        tone = 0.25 * np.sin(np.arange(1600) * 2 * np.pi * 400 / 16000)
        stereo = np.repeat(tone[:, None], 2, axis=1).astype("<f4")
        speech = self.pipeline.get_by_name("speech")
        buffer = self.Gst.Buffer.new_wrapped(stereo.tobytes())
        buffer.pts = speech.get_current_running_time()
        buffer.duration = self.Gst.SECOND // 10
        self.assertEqual(speech.push_buffer(buffer), self.Gst.FlowReturn.OK)
        deadline = time.monotonic() + 2
        while max(reference_peaks, default=0) < 0.2 and time.monotonic() < deadline:
            self.sample()
        self.assertGreater(max(reference_peaks, default=0), 0.2)

    def test_a_link_failure_restores_the_original_audio_path(self):
        # Unsupported channel count makes the inserted caps incompatible with
        # WebRTC. A failed setup must not leave capture or playback disconnected.
        self.audio.CHANNELS = 0
        with self.assertRaisesRegex(RuntimeError, "Cannot link software AEC"):
            enable_software_aec(self.audio)
        self.assertIsNone(self.pipeline.get_by_name("moss_echo_dsp"))
        self.assertIsNone(self.pipeline.get_by_name("moss_echo_reference"))
        self.pipeline.set_state(self.Gst.State.PLAYING)
        self.sample()

    def test_capture_continues_after_the_sdk_style_playback_flush(self):
        enable_software_aec(self.audio)
        self.pipeline.set_state(self.Gst.State.PLAYING)
        first, _ = self.sample()
        speech = self.pipeline.get_by_name("speech")
        self.pipeline.set_state(self.Gst.State.PAUSED)
        speech.send_event(self.Gst.Event.new_flush_start())
        speech.send_event(self.Gst.Event.new_flush_stop(True))
        self.pipeline.set_state(self.Gst.State.PLAYING)
        sample, values = self.sample()
        self.assertGreater(sample.get_buffer().pts, first.get_buffer().pts)
        self.assertGreater(float(np.sqrt(np.mean(values * values))), 0.05)


if __name__ == "__main__":
    unittest.main()
