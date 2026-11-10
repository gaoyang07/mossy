"""Audio format conversion between the Reachy Mini media stack and MOSS-Live.

Reachy Mini records and plays float32 at 16 kHz (``AudioBase.SAMPLE_RATE``).
MOSS-Live wants mono int16 at 16 kHz on the microphone side. On the speech side
it returns mono int16 at 16 kHz when asked, which only needs a format change,
or at its 48 kHz default, which needs a 3:1 decimation.
"""

import numpy as np
import numpy.typing as npt

from .live_client import TTS_SAMPLE_RATE

# reachy_mini.media.audio_base.AudioBase.SAMPLE_RATE — the rate both
# get_audio_sample() and push_audio_sample() work in.
REACHY_SAMPLE_RATE = 16000


def mic_to_pcm(sample: npt.NDArray[np.float32]) -> bytes:
    """Convert one recorded chunk to the mono int16 LE the server expects.

    Args:
        sample: ``[N, channels]`` float32 as returned by ``media.get_audio_sample()``.

    """
    mono = sample[:, 0]  # [N]
    return (np.clip(mono, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()


def pcm_to_float(pcm: bytes) -> npt.NDArray[np.float32]:
    """Decode mono int16 LE speech that is already at Reachy's sample rate."""
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0  # [N]


class SpeechDecimator:
    """Streaming 48 kHz -> 16 kHz decimator for the TTS audio stream.

    ``scipy.signal.resample_poly`` restarts its filter for every call, which
    leaves an audible transient at each chunk boundary — and MOSS-Live sends a
    chunk roughly every 100 ms. Carrying the filter state and the decimation
    phase across chunks keeps the stream continuous instead.
    """

    def __init__(self, factor: int = TTS_SAMPLE_RATE // REACHY_SAMPLE_RATE) -> None:
        # scipy.signal doubles the native libraries the app loads at startup and
        # is not needed when the server already sends 16 kHz.
        from scipy.signal import firwin, lfilter

        self.factor = factor
        self._lfilter = lfilter
        # Same design resample_poly uses: a Kaiser-windowed FIR cutting at the
        # output Nyquist, half-length 10 samples per phase.
        self._taps = firwin(2 * 10 * factor + 1, 1.0 / factor, window=("kaiser", 5.0)).astype(
            np.float64
        )
        self.reset()

    def reset(self) -> None:
        """Drop the filter tail and re-anchor the phase (use on barge-in)."""
        self._zi = np.zeros(len(self._taps) - 1, dtype=np.float64)
        self._phase = 0

    def __call__(self, pcm: bytes) -> npt.NDArray[np.float32]:
        """Decode one mono int16 LE chunk at 48 kHz to float32 at 16 kHz."""
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768.0  # [N]
        filtered, self._zi = self._lfilter(self._taps, 1.0, samples, zi=self._zi)
        out = filtered[self._phase :: self.factor]
        self._phase = (self._phase - len(samples)) % self.factor
        return out.astype(np.float32)  # [N // factor]
