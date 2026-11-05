"""MOSS - Main entry point for the Reachy Mini conversational AI app.

The robot is a microphone and a speaker for MOSS-Live: recorded audio goes
straight to the server, the speech that comes back is played on the robot, and
the whole conversation (VAD, ASR, LLM, TTS) happens server-side.
"""

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from reachy_mini.apps.app import ReachyMiniApp
from reachy_mini.reachy_mini import ReachyMini

from .audio import SpeechDecimator, mic_to_pcm
from .config import (
    API_KEY,
    AUDIO_GAIN,
    EXPRESSION_INTENSITY,
    EXPRESSIONS_ENABLED,
    HALF_DUPLEX,
    LIVE_INSTRUCTIONS,
    LIVE_MODEL,
    LIVE_NAME,
    LIVE_PROMPT,
    LIVE_URL,
    LIVE_USER,
    LIVE_VERIFY_TLS,
    WEB_UI_PORT,
)
from .expressions import Expressions
from .live_client import MossLive

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# How long the microphone stays shut after the robot stops speaking, so the tail
# of the speaker (and the room) is not picked up as the next user turn.
MIC_REOPEN_DELAY = 0.35

TRANSCRIPT_LIMIT = 100


class Moss(ReachyMiniApp):
    """MOSS conversational AI app for Reachy Mini."""

    custom_app_url = f"http://0.0.0.0:{WEB_UI_PORT}"
    dont_start_webserver = True

    def __init__(self) -> None:
        super().__init__()
        self.reachy: ReachyMini | None = None
        self.state = "disconnected"
        self.transcript: list[dict[str, Any]] = []
        self.muted = False
        self._expressions: Expressions | None = None

        self._transcript_lock = threading.Lock()
        self._decimator = SpeechDecimator()
        self._mic_open_at = 0.0
        self._live = MossLive(
            LIVE_URL,
            api_key=API_KEY,
            model=LIVE_MODEL,
            user=LIVE_USER,
            verify_tls=LIVE_VERIFY_TLS,
            name=LIVE_NAME,
            prompt=LIVE_PROMPT,
            instructions=LIVE_INSTRUCTIONS,
            on_audio=self._on_audio,
            on_flush=self._on_flush,
            on_state=self._on_state,
            on_text=self._on_text,
            on_event=self._on_event,
        )

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        self.reachy = reachy_mini

        reachy_mini.enable_motors()
        time.sleep(0.2)
        reachy_mini.wake_up()
        reachy_mini.enable_wobbling()
        self._expressions = Expressions(
            reachy_mini, enabled=EXPRESSIONS_ENABLED, intensity=EXPRESSION_INTENSITY
        )
        self._expressions.start()

        # Capture and playback share one GStreamer pipeline, so they are started
        # together here and stopped together on the way out — stopping either one
        # stops both.
        reachy_mini.media.start_playing()
        reachy_mini.media.start_recording()

        threading.Thread(target=self._run_server, daemon=True).start()
        live_thread = threading.Thread(target=self._run_live, args=(stop_event,))
        live_thread.start()

        if not LIVE_VERIFY_TLS:
            logger.warning("TLS verification disabled for %s", LIVE_URL)
        logger.info(f"MOSS is running. Web UI at http://localhost:{WEB_UI_PORT}")

        try:
            self._pump_microphone(stop_event)
        finally:
            stop_event.set()
            live_thread.join(timeout=10)
            self._expressions.stop()
            reachy_mini.disable_wobbling()
            # goto_sleep() plays its own sound through a separate playbin, which
            # would fight our pipeline for the audio device if it were still up.
            reachy_mini.media.stop_playing()
            reachy_mini.goto_sleep()
            reachy_mini.disable_motors()

    # --- audio --------------------------------------------------------------

    def _pump_microphone(self, stop_event: threading.Event) -> None:
        """Forward recorded audio to MOSS-Live until the app is stopped.

        ``get_audio_sample()`` blocks for up to 20 ms, which paces this loop.
        """
        assert self.reachy is not None
        media = self.reachy.media

        while not stop_event.is_set():
            sample = media.get_audio_sample()  # [N, 2] float32 @ 16 kHz
            if sample is None:
                continue
            if self._mic_closed():
                # Keep the realtime input clock moving without feeding speaker
                # echo (or muted microphone content) back to the model.
                self._live.submit_mic(bytes(sample.shape[0] * 2))
                continue
            self._live.submit_mic(mic_to_pcm(sample))

    def _mic_closed(self) -> bool:
        if self.muted:
            return True
        if not HALF_DUPLEX:
            return False
        if self._live.is_playing:
            self._mic_open_at = time.monotonic() + MIC_REOPEN_DELAY
            return True
        return time.monotonic() < self._mic_open_at

    def _on_audio(self, pcm: bytes) -> None:
        """Play one chunk of MOSS-Live speech (mono int16 LE @ 48 kHz)."""
        if self.reachy is None:
            return
        samples = self._decimator(pcm)  # [N] float32 @ 16 kHz
        if AUDIO_GAIN != 1.0:
            samples = np.clip(samples * AUDIO_GAIN, -1.0, 1.0).astype(np.float32)
        self.reachy.media.push_audio_sample(samples)

    def _on_flush(self) -> None:
        """Barge-in: drop audio the robot has not spoken yet."""
        self._decimator.reset()
        if self.reachy is not None and self.reachy.media.audio is not None:
            self.reachy.media.audio.clear_player()

    # --- conversation state -------------------------------------------------

    def _on_state(self, state: str) -> None:
        self.state = state

    def _on_event(self, action: str, data: dict[str, Any]) -> None:
        if self._expressions is not None:
            self._expressions.submit(action, data)

    def _on_text(self, role: str, text: str, final: bool) -> None:
        """Record a transcript line, replacing the one still being streamed."""
        with self._transcript_lock:
            if (
                self.transcript
                and self.transcript[-1]["role"] == role
                and not self.transcript[-1]["final"]
            ):
                self.transcript[-1] = {"role": role, "text": text, "final": final}
            else:
                self.transcript.append({"role": role, "text": text, "final": final})
            del self.transcript[:-TRANSCRIPT_LIMIT]

    # --- MOSS-Live session --------------------------------------------------

    def _run_live(self, stop_event: threading.Event) -> None:
        async def main() -> None:
            stop = asyncio.Event()

            async def watch_stop() -> None:
                while not stop_event.is_set():
                    await asyncio.sleep(0.2)
                stop.set()

            watcher = asyncio.create_task(watch_stop())
            try:
                await self._live.run(stop)
            finally:
                watcher.cancel()

        try:
            asyncio.run(main())
        except Exception:
            logger.exception("MOSS-Live client stopped")

    # --- web UI -------------------------------------------------------------

    def _run_server(self) -> None:
        import uvicorn

        app = FastAPI()

        @app.get("/")
        async def index():
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/state")
        async def state():
            with self._transcript_lock:
                transcript = list(self.transcript)
            return JSONResponse(
                {
                    "state": self.state,
                    "connected": self._live.connected,
                    "muted": self.muted,
                    "session_id": self._live.session_id,
                    "latency": self._live.latency,
                    "error": self._live.last_error,
                    "full_duplex": not HALF_DUPLEX,
                    "expressions": self._expressions.snapshot() if self._expressions else None,
                    "transcript": transcript,
                }
            )

        @app.post("/mute")
        async def mute():
            self.muted = not self.muted
            return JSONResponse({"muted": self.muted})

        @app.post("/expressions/toggle")
        async def toggle_expressions():
            if self._expressions is None:
                return JSONResponse({"error": "Robot is not running"}, status_code=503)
            enabled = not self._expressions.snapshot()["enabled"]
            self._expressions.submit("motion.enabled", {"enabled": enabled})
            return JSONResponse({"enabled": enabled})

        uvicorn.run(app, host="0.0.0.0", port=WEB_UI_PORT, log_level="warning")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = Moss()
    app.wrapped_run()


if __name__ == "__main__":
    main()
