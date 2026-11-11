"""MOSS - Main entry point for the Reachy Mini conversational AI app.

Robot microphone audio and optional camera frames go to MOSS-Live. Returned
speech plays on the robot; capture, playback and expressions run independently.
"""

import asyncio
import json
import logging
import signal
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from reachy_mini.apps.app import ReachyMiniApp
from reachy_mini.reachy_mini import INIT_ANTENNAS_JOINT_POSITIONS, INIT_HEAD_POSE, ReachyMini

from . import STARTED_AT, config
from .audio import REACHY_SAMPLE_RATE, SpeechDecimator, mic_to_pcm, pcm_to_float
from .camera import Camera, local_camera_access
from .echo import EchoCancellation
from .expressions import Expressions
from .live_client import MossLive, list_voices

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SETTINGS = config.load()

# How long the microphone stays shut after the robot stops speaking, so the tail
# of the speaker (and the room) is not picked up as the next user turn.
MIC_REOPEN_DELAY = 0.35

TRANSCRIPT_LIMIT = 100

# Settings that take a new MOSS-Live connection, and the argument each becomes.
CONNECTION = {
    "MOSS_API_KEY": "api_key",
    "MOSS_LIVE_URL": "base_url",
    "MOSS_LIVE_MODEL": "model",
    "MOSS_LIVE_USER": "user",
    "MOSS_LIVE_VERIFY_TLS": "verify_tls",
    "MOSS_LIVE_OUTPUT_RATE": "output_rate",
}


class Moss(ReachyMiniApp):
    """MOSS conversational AI app for Reachy Mini."""

    custom_app_url = f"http://0.0.0.0:{SETTINGS['MOSS_WEB_UI_PORT']}"
    dont_start_webserver = True

    def __init__(self) -> None:
        super().__init__()
        self.settings = dict(SETTINGS)
        self.reachy: ReachyMini | None = None
        self.state = "disconnected"
        self.transcript: list[dict[str, Any]] = []
        self.muted = False
        self._expressions: Expressions | None = None
        self._camera: Camera | None = None
        self._echo: EchoCancellation | None = None

        self._transcript_lock = threading.Lock()
        self._transcript_version = 0
        self._settings_lock = threading.Lock()
        self._decimator: SpeechDecimator | None = None
        # One thread hands speech to the audio device, in arrival order, so that
        # neither resampling nor a slow flush holds up the MOSS-Live socket.
        self._speaker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="moss-speaker")
        self._speaker_open = False
        self._speaker_epoch = 0
        self._mic_open_at = 0.0
        self._last_activity = time.monotonic()
        self._ready_s: float | None = None
        settings = self.settings
        self._live = MossLive(
            settings["MOSS_LIVE_URL"],
            api_key=settings["MOSS_API_KEY"],
            model=settings["MOSS_LIVE_MODEL"],
            voice_id=settings["MOSS_LIVE_VOICE_ID"],
            user=settings["MOSS_LIVE_USER"],
            verify_tls=settings["MOSS_LIVE_VERIFY_TLS"],
            name=settings["MOSS_LIVE_NAME"],
            prompt=settings["MOSS_LIVE_PROMPT"],
            instructions=settings["MOSS_LIVE_INSTRUCTIONS"],
            camera_enabled=settings["MOSS_CAMERA"],
            output_rate=settings["MOSS_LIVE_OUTPUT_RATE"],
            speed=settings["MOSS_LIVE_SPEED"],
            volume_db=settings["MOSS_LIVE_VOLUME_DB"],
            time_zone=_time_zone(settings),
            on_audio=self._on_audio,
            on_flush=self._on_flush,
            on_state=self._on_state,
            on_text=self._on_text,
            on_event=self._on_event,
        )

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        if stop_event.is_set():
            return
        self.reachy = reachy_mini
        settings = self.settings
        self._camera = Camera(
            reachy_mini.media.get_frame,
            self._live.submit_camera,
            self._live.set_camera_enabled,
            enabled=settings["MOSS_CAMERA"],
            fps=settings["MOSS_CAMERA_FPS"],
            max_edge=settings["MOSS_CAMERA_MAX_EDGE"],
            quality=settings["MOSS_CAMERA_JPEG_QUALITY"],
        )
        self._expressions = Expressions(
            reachy_mini,
            enabled=settings["MOSS_EXPRESSIONS"],
            intensity=settings["MOSS_EXPRESSION_INTENSITY"],
        )
        self._echo = EchoCancellation(
            reachy_mini.media.audio, software=settings["MOSS_SOFTWARE_AEC"]
        )

        # The page, the MOSS-Live handshake and the gesture recordings do not
        # need the robot awake, so they get ready while it wakes up.
        threading.Thread(target=self._run_server, daemon=True).start()
        live_thread = threading.Thread(target=self._run_live, args=(stop_event,))
        live_thread.start()
        self._expressions.start()
        if not settings["MOSS_LIVE_VERIFY_TLS"]:
            logger.warning("TLS verification disabled for %s", settings["MOSS_LIVE_URL"])
        if settings["MOSS_LIVE_OUTPUT_RATE"] != REACHY_SAMPLE_RATE:
            # Build the resampler now rather than when the first reply arrives.
            self._decimate(settings["MOSS_LIVE_OUTPUT_RATE"])

        try:
            reachy_mini.enable_motors()
            time.sleep(0.2)
            if settings["MOSS_WAKE_ANIMATION"]:
                reachy_mini.wake_up()
            else:
                reachy_mini.goto_target(
                    INIT_HEAD_POSE, antennas=INIT_ANTENNAS_JOINT_POSITIONS, duration=1.0
                )
            reachy_mini.enable_wobbling()
            if settings["MOSS_POSTURE"] != "neutral":  # wake_up() ends in the neutral pose.
                self._expressions.set_posture(settings["MOSS_POSTURE"])

            # Capture and playback share one GStreamer pipeline, so they are started
            # together here and stopped together on the way out — stopping either one
            # stops both. wake_up() plays its sound through a separate playbin, which
            # is why this waits for it.
            reachy_mini.media.start_playing()
            reachy_mini.media.start_recording()
            self._speaker_open = True
            self._camera.start()
            logger.info(
                "MOSS is running. Web UI at http://localhost:%d", settings["MOSS_WEB_UI_PORT"]
            )
            self._pump_microphone(stop_event)
        finally:
            stop_event.set()
            self._camera.stop()
            live_thread.join(timeout=10)
            self._speaker_open = False
            self._speaker.shutdown(wait=True, cancel_futures=True)
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
            if self._ready_s is None and self._live.connected:
                self._ready_s = time.monotonic() - STARTED_AT
                logger.info("Ready to talk %.1f s after launch", self._ready_s)
            idle_limit = self.settings["MOSS_IDLE_SHUTDOWN_MIN"] * 60
            if idle_limit and time.monotonic() - self._last_activity > idle_limit:
                logger.info("No conversation for %g minutes; going to sleep", idle_limit / 60)
                return
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
        if not self.settings["MOSS_HALF_DUPLEX"]:
            return False
        if self._live.is_playing:
            self._mic_open_at = time.monotonic() + MIC_REOPEN_DELAY
            return True
        return time.monotonic() < self._mic_open_at

    def _on_audio(self, pcm: bytes) -> None:
        """Queue one chunk of MOSS-Live speech (mono int16 LE) for the speaker."""
        if self._speaker_open:
            self._speaker.submit(self._play, pcm, self._live.output_rate, self._speaker_epoch)

    def _play(self, pcm: bytes, rate: int, epoch: int) -> None:
        try:
            if epoch != self._speaker_epoch:
                return  # Interrupted while it waited its turn.
            if rate == REACHY_SAMPLE_RATE:
                samples = pcm_to_float(pcm)  # [N] float32 @ 16 kHz
            else:
                samples = self._decimate(rate)(pcm)  # [N] float32 @ 16 kHz
            gain = self.settings["MOSS_AUDIO_GAIN"]
            if gain != 1.0:
                samples = np.clip(samples * gain, -1.0, 1.0).astype(np.float32)
            assert self.reachy is not None
            self.reachy.media.push_audio_sample(samples)
        except Exception:
            logger.exception("Could not play MOSS-Live speech")

    def _decimate(self, rate: int) -> SpeechDecimator:
        factor, remainder = divmod(rate, REACHY_SAMPLE_RATE)
        if remainder:
            raise ValueError(f"Cannot play {rate} Hz speech on a {REACHY_SAMPLE_RATE} Hz device")
        if self._decimator is None or self._decimator.factor != factor:
            self._decimator = SpeechDecimator(factor)
        return self._decimator

    def _on_flush(self) -> None:
        """Barge-in: drop audio the robot has not spoken yet."""
        self._speaker_epoch += 1
        if self._speaker_open:
            self._speaker.submit(self._flush)

    def _flush(self) -> None:
        try:
            if self._decimator is not None:
                self._decimator.reset()
            if self.reachy is not None and self.reachy.media.audio is not None:
                self.reachy.media.audio.clear_player()
        except Exception:
            logger.exception("Could not drop queued speech")

    # --- conversation state -------------------------------------------------

    def _on_state(self, state: str) -> None:
        self.state = state
        if state in ("listening", "speaking"):
            self._last_activity = time.monotonic()

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
            self._transcript_version += 1

    # --- settings -----------------------------------------------------------

    def apply_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        """Apply changes to the running conversation and save them for the next start.

        Anything MOSS-Live has to accept is saved only after it confirms, so a
        rejected voice or prompt cannot break the next start.
        """
        with self._settings_lock:
            changes: dict[str, Any] = {}
            for key, value in values.items():
                if key not in config.FIELDS:
                    raise ValueError(f"Unknown setting: {key}")
                value = config.coerce(key, value)
                if config.FIELDS[key].kind == "secret" and not value:
                    continue  # An empty secret field leaves the stored one alone.
                if value != self.settings[key]:
                    if config.pinned(key):
                        raise ValueError(f"{key} is set by an exported environment variable")
                    changes[key] = value
            new = {**self.settings, **changes}
            live, expressions, camera = self._live, self._expressions, self._camera
            assert expressions is not None and camera is not None
            motion = ("MOSS_EXPRESSIONS", "MOSS_EXPRESSION_INTENSITY", "MOSS_POSTURE")
            if expressions.snapshot()["error"] and any(key in changes for key in motion):
                # Its worker has stopped, so nothing would act on the change.
                raise RuntimeError(
                    f"Expressions are not available: {expressions.snapshot()['error']}"
                )
            steps = [
                (("MOSS_LIVE_VOICE_ID",), lambda: live.change_voice(new["MOSS_LIVE_VOICE_ID"])),
                (
                    ("MOSS_LIVE_NAME", "MOSS_LIVE_PROMPT", "MOSS_LIVE_INSTRUCTIONS"),
                    lambda: live.update_session(
                        new["MOSS_LIVE_NAME"],
                        new["MOSS_LIVE_PROMPT"],
                        new["MOSS_LIVE_INSTRUCTIONS"],
                    ),
                ),
                (
                    ("MOSS_LIVE_VOLUME_DB", "MOSS_LIVE_SPEED"),
                    lambda: live.configure_output(
                        new["MOSS_LIVE_VOLUME_DB"], new["MOSS_LIVE_SPEED"]
                    ),
                ),
                (("MOSS_TIME_ZONE",), lambda: live.update_time_zone(_time_zone(new))),
                (
                    tuple(CONNECTION),
                    lambda: live.reconnect(
                        **{CONNECTION[key]: new[key] for key in CONNECTION if key in changes}
                    ),
                ),
                (
                    ("MOSS_EXPRESSIONS",),
                    lambda: expressions.submit(
                        "motion.enabled", {"enabled": new["MOSS_EXPRESSIONS"]}
                    ),
                ),
                (
                    ("MOSS_EXPRESSION_INTENSITY",),
                    lambda: expressions.set_intensity(new["MOSS_EXPRESSION_INTENSITY"]),
                ),
                (("MOSS_POSTURE",), lambda: expressions.set_posture(new["MOSS_POSTURE"])),
                (
                    ("MOSS_CAMERA_FPS", "MOSS_CAMERA_MAX_EDGE", "MOSS_CAMERA_JPEG_QUALITY"),
                    lambda: camera.configure(
                        fps=new["MOSS_CAMERA_FPS"],
                        max_edge=new["MOSS_CAMERA_MAX_EDGE"],
                        quality=new["MOSS_CAMERA_JPEG_QUALITY"],
                    ),
                ),
                (("MOSS_CAMERA",), lambda: camera.set_enabled(new["MOSS_CAMERA"])),
                # The rest are read where they are used, or at the next start.
                (tuple(config.FIELDS), lambda: None),
            ]
            saved: list[str] = []
            for keys, apply in steps:
                group = {key: changes[key] for key in keys if key in changes and key not in saved}
                if not group:
                    continue
                apply()
                config.save(group)
                self.settings.update(group)
                saved += group
            self._last_activity = time.monotonic()
            return {
                "saved": saved,
                "restart": any(config.FIELDS[key].scope == "restart" for key in saved),
            }

    def _daemon(self, route: str, *, method: str = "GET", timeout: float = 3.0) -> Any:
        """Call the local Reachy daemon that manages this app."""
        assert self.reachy is not None
        client = self.reachy.client
        request = urllib.request.Request(
            f"http://{client.host}:{client.port}{route}", method=method
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)

    def restart(self) -> None:
        """Have the daemon stop this process and start a fresh one."""
        current = self._daemon("/api/apps/current-app-status")
        if not current or current["info"]["name"] != "mossy":
            raise RuntimeError("This process was not started by the Reachy daemon")

        def request() -> None:
            try:
                self._daemon("/api/apps/restart-current-app", method="POST", timeout=60)
            except Exception:
                pass  # The daemon stops this process before it can answer.

        threading.Thread(target=request, daemon=True).start()

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

        def local(request: Request) -> bool:
            return local_camera_access(
                request.client.host if request.client else None,
                request.headers.get("host", ""),
                request.headers.get("origin"),
                request.headers.get("sec-fetch-site"),
            )

        def refuse(message: str, status: int) -> JSONResponse:
            return JSONResponse({"error": message}, status_code=status)

        @app.get("/")
        async def index():
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.get("/state")
        async def state(request: Request, version: int = -1):
            with self._transcript_lock:
                # A page that already has this transcript does not need it again.
                unchanged = version == self._transcript_version
                transcript = None if unchanged else list(self.transcript)
                version = self._transcript_version
            return JSONResponse(
                {
                    "state": self.state,
                    "connected": self._live.connected,
                    "muted": self.muted,
                    "session_id": self._live.session_id,
                    "voice_id": self._live.voice_id,
                    "stats": {**self._live.stats(), "ready_s": self._ready_s},
                    "error": self._live.last_error,
                    "full_duplex": not self.settings["MOSS_HALF_DUPLEX"],
                    "echo_cancellation": self._echo.snapshot() if self._echo else None,
                    "expressions": self._expressions.snapshot() if self._expressions else None,
                    "camera": (
                        {**self._camera.snapshot(), **self._live.camera_status()}
                        if self._camera
                        else None
                    ),
                    "local_access": local(request),
                    "transcript": transcript,
                    "transcript_version": version,
                }
            )

        # Everything that changes something answers only a browser on this
        # machine. The page itself and /state stay readable from the network.

        @app.post("/mute")
        async def mute(request: Request):
            if not local(request):
                return refuse("Microphone controls require local access", 403)
            self.muted = not self.muted
            return JSONResponse({"muted": self.muted})

        @app.post("/expressions/toggle")
        async def toggle_expressions(request: Request):
            if not local(request):
                return refuse("Expression controls require local access", 403)
            if self._expressions is None:
                return JSONResponse({"error": "Robot is not running"}, status_code=503)
            enabled = not self._expressions.snapshot()["enabled"]
            self._expressions.submit("motion.enabled", {"enabled": enabled})
            return JSONResponse({"enabled": enabled})

        @app.post("/camera/toggle")
        async def toggle_camera(request: Request):
            if not local(request):
                return JSONResponse(
                    {"error": "Camera controls require local access"}, status_code=403
                )
            if self._camera is None:
                return JSONResponse({"error": "Robot is not running"}, status_code=503)
            enabled = not self._camera.snapshot()["enabled"]
            self._camera.set_enabled(enabled)
            return JSONResponse({"enabled": enabled})

        @app.get("/camera/frame")
        async def camera_frame(request: Request):
            if not local(request):
                return Response(status_code=403)
            jpeg = self._camera.preview() if self._camera is not None else None
            if jpeg is None:
                return Response(status_code=204, headers={"Cache-Control": "no-store"})
            return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

        # The handlers below are plain functions, so the ones that wait for
        # MOSS-Live run in the server's thread pool.

        @app.get("/api/settings")
        def read_settings(request: Request):
            if not local(request):
                return refuse("Settings require local access", 403)
            return JSONResponse(
                {
                    "groups": config.GROUPS,
                    "fields": config.describe(self.settings),
                    "local_time_zone": config.local_time_zone(),
                }
            )

        @app.post("/api/settings")
        def write_settings(request: Request, values: dict[str, Any]):
            if not local(request):
                return refuse("Settings require local access", 403)
            try:
                return JSONResponse(self.apply_settings(values))
            except ValueError as exc:
                return refuse(str(exc), 400)
            except (RuntimeError, TimeoutError, ConnectionError) as exc:
                return refuse(str(exc), 502)

        @app.get("/api/voices")
        def voices(request: Request):
            if not local(request):
                return refuse("Settings require local access", 403)
            settings = self.settings
            try:
                found = list_voices(
                    settings["MOSS_LIVE_URL"],
                    settings["MOSS_API_KEY"],
                    settings["MOSS_LIVE_VERIFY_TLS"],
                )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}".replace(settings["MOSS_API_KEY"], "…")
                return refuse(message, 502)
            return JSONResponse({"voices": found})

        @app.post("/api/gesture/{kind}")
        def gesture(kind: str, request: Request):
            if not local(request):
                return refuse("Robot controls require local access", 403)
            if self._expressions is None or not self._expressions.snapshot()["available"]:
                return refuse("Expressions are not available", 503)
            try:
                self._expressions.preview(kind)
            except ValueError as exc:
                return refuse(str(exc), 400)
            return JSONResponse({"kind": kind})

        @app.post("/api/app/{action}")
        def control(action: str, request: Request):
            if not local(request):
                return refuse("App controls require local access", 403)
            if action == "stop":
                self.stop_event.set()
            elif action == "restart":
                try:
                    self.restart()
                except Exception as exc:
                    return refuse(str(exc), 409)
            else:
                return refuse(f"Unknown action: {action}", 404)
            return JSONResponse({"action": action})

        uvicorn.run(
            app,
            host="0.0.0.0",
            port=self.settings["MOSS_WEB_UI_PORT"],
            log_level="warning",
            ws="none",  # The page polls; skip loading a WebSocket server stack.
        )


def _time_zone(settings: dict[str, Any]) -> str:
    zone = settings["MOSS_TIME_ZONE"]
    return config.local_time_zone() if zone == "auto" else zone


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = Moss()

    def stop(signum: int, frame: Any) -> None:
        if app.stop_event.is_set():
            raise KeyboardInterrupt  # A second Ctrl+C skips the graceful shutdown.
        app.stop_event.set()

    # The Reachy daemon stops apps with SIGINT; let the robot go to sleep first.
    signal.signal(signal.SIGINT, stop)
    app.wrapped_run()


if __name__ == "__main__":
    main()
