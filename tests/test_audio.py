"""Speech format conversion, and what it costs the app to load it."""

import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np

from mossy.audio import SpeechDecimator, mic_to_pcm, pcm_to_float


class ConversionTests(unittest.TestCase):
    def test_speech_at_the_robot_rate_only_changes_format(self):
        samples = pcm_to_float(np.array([0, 16384, -32768, 32767], dtype="<i2").tobytes())
        self.assertEqual(samples.dtype, np.float32)
        np.testing.assert_allclose(samples, [0.0, 0.5, -1.0, 32767 / 32768])

    def test_microphone_audio_becomes_clipped_mono_int16(self):
        stereo = np.array([[0.5, -0.5], [2.0, 0.0], [-2.0, 0.0]], dtype=np.float32)
        pcm = np.frombuffer(mic_to_pcm(stereo), dtype="<i2")
        np.testing.assert_array_equal(pcm, [16383, 32767, -32767])

    def test_decimation_is_continuous_across_chunks_of_any_size(self):
        tone = np.sin(np.arange(9600) * 2 * np.pi * 440 / 48000)
        pcm = (tone * 20000).astype("<i2").tobytes()
        whole = SpeechDecimator()(pcm)
        self.assertEqual(len(whole), 3200)
        chunked = SpeechDecimator()
        pieces = [
            chunked(pcm[start:end]) for start, end in ((0, 2000), (2000, 2006), (2006, 19200))
        ]
        np.testing.assert_allclose(np.concatenate(pieces), whole, atol=1e-6)
        # A 440 Hz tone passes the low-pass filter at close to full scale.
        self.assertAlmostEqual(float(np.abs(whole[200:]).max()), 20000 / 32768, delta=0.02)

    def test_reset_forgets_the_interrupted_reply(self):
        loud = (np.ones(4800) * 20000).astype("<i2").tobytes()
        decimator = SpeechDecimator()
        decimator(loud)
        decimator.reset()
        np.testing.assert_allclose(decimator(bytes(960)), np.zeros(160), atol=1e-12)


class StartupCostTests(unittest.TestCase):
    def test_the_resampler_library_loads_only_when_48_khz_speech_needs_it(self):
        # scipy.signal doubles the native libraries loaded at startup; with
        # 16 kHz speech from the server nothing here needs it.
        script = (
            "import sys; import mossy.audio, mossy.live_client, mossy.config; "
            "assert 'scipy.signal' not in sys.modules, 'scipy.signal loaded at import'; "
            "mossy.audio.SpeechDecimator(); assert 'scipy.signal' in sys.modules"
        )
        source = str(Path(__file__).resolve().parents[1] / "src")
        result = subprocess.run(
            [sys.executable, "-c", script],
            env={"PYTHONPATH": source},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
