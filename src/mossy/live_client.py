"""Realtime media client for https://platform.moss.cn/docs/reference/moss-live/.

Authenticate in the WebSocket handshake, attach and configure a session, then
stream tagged 16 kHz PCM and optional camera JPEGs on one socket. Downlink audio
remains untagged 48 kHz PCM. Tagging stays enabled when the camera is turned off.
"""

import asyncio
import json
import logging
import ssl
import time
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import websockets

logger = logging.getLogger(__name__)

MIC_SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 48000
_RECONNECT_DELAYS = (0.0, 1.0, 2.0, 5.0, 5.0)
_MIC_QUEUE_MAX = 200  # About four seconds of 20 ms microphone chunks.
_CAMERA_MAX_AGE = 3.0


class MossLive:
    """One conversation; only ``submit_mic`` is called from another thread."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str,
        model: str = "moss-live-1.0",
        voice_id: str = "",
        user: str = "reachy-mini",
        verify_tls: bool = True,
        name: str = "MOSS",
        prompt: str = "",
        instructions: str = "",
        camera_enabled: bool = False,
        on_audio: Callable[[bytes], None],
        on_flush: Callable[[], None],
        on_state: Callable[[str], None],
        on_text: Callable[[str, str, bool], None],
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Set MOSS_API_KEY in ~/.config/mossy/.env or the environment")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._voice_id = voice_id.strip()
        self._user = user
        self._verify_tls = verify_tls
        self._persona = {"name": name}
        if prompt:
            self._persona["prompt"] = prompt
        self._instructions = instructions
        self._camera_enabled = camera_enabled
        self._on_audio = on_audio
        self._on_flush = on_flush
        self._on_state = on_state
        self._on_text = on_text
        self._on_event = on_event

        self.session_id: str | None = None
        self.voice_id: str | None = None
        self.connected = False
        self.is_playing = False
        self.latency: dict[str, int] = {}
        self.last_error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._mic: asyncio.Queue[bytes] | None = None
        self._camera_frame: tuple[bytes, float] | None = None
        self._camera_wake: asyncio.Event | None = None
        self._camera_announced: bool | None = None
        self._camera_generation = 0
        self._camera_frames_sent = 0
        self._camera_sent_at: float | None = None
        self._send_lock = asyncio.Lock()
        self._chunk_meta: deque[dict[str, str]] = deque()
        self._play_queue: asyncio.Queue[tuple[dict[str, str], bytes]] | None = None
        self._scope: dict[str, str] | None = None
        self._playing_scope: dict[str, str] | None = None
        self._pending_finish: dict[str, str] | None = None
        self._chunk_active = False
        self._chunk_started_at: float | None = None
        self._chunk_duration = 0.0
        self._interrupted_scope: dict[str, str] | None = None
        self._playback_generation = 0
        self._playback_interrupted = asyncio.Event()

    def submit_mic(self, pcm: bytes) -> None:
        """Submit mono int16 LE at 16 kHz; drop input until configuration succeeds."""
        loop = self._loop
        if loop is None or self._mic is None or not self.connected:
            return
        loop.call_soon_threadsafe(self._enqueue_mic, pcm)

    def _enqueue_mic(self, pcm: bytes) -> None:
        assert self._mic is not None
        if not self.connected:
            return
        while self._mic.qsize() >= _MIC_QUEUE_MAX:
            self._mic.get_nowait()
        self._mic.put_nowait(pcm)

    def set_camera_enabled(self, enabled: bool) -> None:
        """Change camera input without reconnecting or changing audio framing."""
        loop = self._loop
        if loop is None or loop.is_closed():
            self._set_camera_enabled(enabled)
        else:
            try:
                loop.call_soon_threadsafe(self._set_camera_enabled, enabled)
            except RuntimeError:
                # A concurrent shutdown may have closed the loop after the check.
                self._set_camera_enabled(enabled)

    def _set_camera_enabled(self, enabled: bool) -> None:
        self._camera_enabled = enabled
        self._camera_generation += 1
        self._camera_frame = None
        if self._camera_wake is not None:
            self._camera_wake.set()

    def submit_camera(self, jpeg: bytes, captured_at: float) -> None:
        """Submit a robot JPEG from another thread; retain only the latest frame."""
        loop = self._loop
        if loop is None or not self.connected or not self._camera_enabled:
            return
        loop.call_soon_threadsafe(self._enqueue_camera, jpeg, captured_at, self._camera_generation)

    def _enqueue_camera(self, jpeg: bytes, captured_at: float, generation: int) -> None:
        if not self.connected or not self._camera_enabled or generation != self._camera_generation:
            return
        self._camera_frame = (jpeg, captured_at)
        assert self._camera_wake is not None
        self._camera_wake.set()

    def camera_status(self) -> dict[str, Any]:
        age = time.monotonic() - self._camera_sent_at if self._camera_sent_at is not None else None
        return {
            "sent_frames": self._camera_frames_sent,
            "last_sent_age_s": round(age, 2) if age is not None else None,
        }

    def _reset_camera(self) -> None:
        self._camera_generation += 1
        self._camera_frame = None
        self._camera_announced = None
        self._camera_sent_at = None
        if self._camera_wake is not None:
            self._camera_wake.clear()

    async def run(self, stop: asyncio.Event) -> None:
        """Keep the session up until stopped, clearing playback on disconnect."""
        self._loop = asyncio.get_running_loop()
        self._mic = asyncio.Queue()
        self._camera_wake = asyncio.Event()
        self._play_queue = asyncio.Queue()
        attempt = 0
        try:
            while not stop.is_set():
                delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
                if delay:
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=delay)
                        break
                    except asyncio.TimeoutError:
                        pass
                ready = False
                try:
                    ready = await self._session(stop)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Keep errors visible without exposing a credential if a
                    # transport exception includes request details.
                    self.last_error = f"{type(exc).__name__}: {exc}".replace(
                        self._api_key, "[redacted]"
                    )
                    logger.warning("MOSS-Live session ended: %s", self.last_error)
                finally:
                    self.connected = False
                    self.voice_id = None
                    self._reset_camera()
                    self._emit("session.disconnected", {})
                    self._on_flush()
                    self._reset_playback()
                    self._on_state("disconnected")
                attempt = 0 if ready else attempt + 1
        finally:
            self._loop = None

    async def _session(self, stop: asyncio.Event) -> bool:
        async with websockets.connect(
            self._ws_url(),
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            ssl=self._ssl_context(),
            max_size=None,
            open_timeout=15,
            ping_interval=20,
            proxy=None,
        ) as ws:
            self.voice_id = None
            self._reset_playback()
            self._reset_camera()
            self._drain_mic()
            attach: dict[str, Any] = {"action": "attach_session"}
            if self.session_id:
                attach["session_id"] = self.session_id
            await self._send(ws, attach)
            ready = asyncio.Event()
            tasks = [
                asyncio.create_task(self._receive(ws, ready)),
                asyncio.create_task(self._forward_mic(ws, ready)),
                asyncio.create_task(self._forward_camera(ws, ready)),
                asyncio.create_task(self._pace_playback(ws)),
                asyncio.create_task(stop.wait()),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            return ready.is_set()

    def _ws_url(self) -> str:
        if self._base_url.startswith("https://"):
            base = "wss://" + self._base_url[len("https://") :]
        elif self._base_url.startswith("http://"):
            base = "ws://" + self._base_url[len("http://") :]
        else:
            raise ValueError("MOSS_LIVE_URL must start with http:// or https://")
        query = {"model": self._model}
        if self._user:
            query["user"] = self._user
        return f"{base}/v1/realtime?{urlencode(query)}"

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self._base_url.startswith("https://"):
            return None
        context = ssl.create_default_context()
        if not self._verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def _send(self, ws: Any, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await ws.send(json.dumps(payload))

    async def _configure_session(self, ws: Any) -> None:
        await self._send(
            ws,
            {
                "action": "session.update",
                "session": {"persona": self._persona, "instructions": self._instructions},
            },
        )

    async def _forward_mic(self, ws: Any, ready: asyncio.Event) -> None:
        assert self._mic is not None
        await asyncio.wait_for(ready.wait(), timeout=30)
        while True:
            chunk = await self._mic.get()
            async with self._send_lock:
                await ws.send(b"\x00" + chunk)

    async def _sync_camera_locked(self, ws: Any) -> None:
        enabled = self._camera_enabled
        if self._camera_announced == enabled:
            return
        await ws.send(json.dumps({"action": "video.state", "source": "camera", "enabled": enabled}))
        if enabled:
            await ws.send(json.dumps({"action": "video.stream_focus", "source": "camera"}))
        self._camera_announced = enabled

    async def _forward_camera(self, ws: Any, ready: asyncio.Event) -> None:
        assert self._camera_wake is not None
        await asyncio.wait_for(ready.wait(), timeout=30)
        while True:
            await self._camera_wake.wait()
            self._camera_wake.clear()
            async with self._send_lock:
                await self._sync_camera_locked(ws)
                frame, self._camera_frame = self._camera_frame, None
                if not self._camera_enabled or frame is None:
                    continue
                jpeg, captured_at = frame
                if time.monotonic() - captured_at > _CAMERA_MAX_AGE:
                    continue
                await ws.send(b"\x01" + jpeg)
                self._camera_frames_sent += 1
                self._camera_sent_at = time.monotonic()

    def _drain_mic(self) -> None:
        assert self._mic is not None
        while not self._mic.empty():
            self._mic.get_nowait()

    async def _receive(self, ws: Any, ready: asyncio.Event) -> None:
        assert self._play_queue is not None
        async for message in ws:
            if isinstance(message, bytes):
                meta = self._chunk_meta.popleft() if self._chunk_meta else None
                if meta is None:
                    raise RuntimeError("Audio arrived without response.audio.context")
                if _scope_of(meta) == self._interrupted_scope:
                    continue
                self._scope = _scope_of(meta)
                self.is_playing = True
                self._on_state("speaking")
                self._play_queue.put_nowait((meta, message))
                continue
            frame = json.loads(message)
            await self._handle(ws, frame.get("action"), frame.get("data") or {}, ready)

    async def _handle(self, ws: Any, action: str | None, data: Any, ready: asyncio.Event) -> None:
        if action in (
            "input.transcript.updated",
            "input.transcript.done",
            "output.transcript.updated",
            "output.transcript.done",
            "response.interrupted",
            "response.audio.stop",
            "response.ended",
        ):
            self._emit(action, data)
        if action == "session_attached":
            self.session_id = data.get("session_id")
            logger.info("Attached to MOSS-Live session %s", self.session_id)
            if self._voice_id:
                await self._send(ws, {"action": "change_voice", "voice_name": self._voice_id})
            else:
                await self._configure_session(ws)
        elif action == "voice_changed":
            voice_id = data.get("name")
            if self._voice_id and voice_id != self._voice_id:
                raise RuntimeError(f"Requested voice {self._voice_id}, server confirmed {voice_id}")
            self.voice_id = voice_id
            logger.info("MOSS-Live voice confirmed: %s", voice_id)
            if self._voice_id and not ready.is_set():
                await self._configure_session(ws)
        elif action == "session.updated":
            if data.get("error"):
                raise RuntimeError(f"Session configuration failed: {data['error']}")
            if self._voice_id and self.voice_id != self._voice_id:
                raise RuntimeError("Session configured before the requested voice was confirmed")
            await self._send(ws, {"action": "media.mode", "binary_protocol": "tagged-v1"})
            async with self._send_lock:
                await self._sync_camera_locked(ws)
            await self._send(ws, {"action": "mic_state", "enabled": True})
            self._drain_mic()
            self.connected = True
            self.last_error = None
            ready.set()
            assert self._camera_wake is not None
            self._camera_wake.set()
            self._on_state("idle")
            logger.info("MOSS-Live session configured; microphone enabled")
        elif action in ("input.transcript.updated", "input.transcript.done"):
            self._on_state("listening")
            self._on_text("user", data.get("text", ""), action.endswith(".done"))
        elif action in (
            "output.transcript.updated",
            "output.transcript.done",
            "response.interrupted",
        ):
            self._on_text("assistant", data.get("text", ""), action != "output.transcript.updated")
        elif action == "response.audio.context":
            meta = _chunk_of(data)
            if meta is None:
                raise RuntimeError("Invalid response.audio.context")
            self._chunk_meta.append(meta)
        elif action == "response.audio.done":
            scope = _scope_of(data)
            if scope != self._interrupted_scope:
                await self._finish_playback(ws, scope)
        elif action == "response.audio.stop":
            await self._interrupt_playback(ws, data)
        elif action == "response.ended":
            logger.info("MOSS-Live response ended: %s", data.get("status"))
            if not self.is_playing:
                self._on_state("idle")
        elif action == "error":
            raise RuntimeError(f"MOSS-Live error: {data.get('error', data)}")
        else:
            logger.debug("Unhandled MOSS-Live action: %s", action)

    async def _pace_playback(self, ws: Any) -> None:
        """Play chunks in order and acknowledge after their playback duration.

        Reachy's SDK exposes enqueue, not a hardware completion callback. This
        clock estimates completion from PCM duration; it does not measure the
        device's output latency.
        """
        assert self._play_queue is not None
        loop = asyncio.get_running_loop()
        while True:
            meta, pcm = await self._play_queue.get()
            duration = len(pcm) / 2 / TTS_SAMPLE_RATE
            generation = self._playback_generation
            self._chunk_active = True
            self._playback_interrupted.clear()
            self._on_audio(pcm)
            scope = _scope_of(meta)
            if scope != self._playing_scope:
                self._playing_scope = scope
                self._emit("playback.started", scope)
            self._chunk_started_at = loop.time()
            self._chunk_duration = duration
            try:
                await asyncio.wait_for(self._playback_interrupted.wait(), timeout=duration)
            except asyncio.TimeoutError:
                pass
            if generation != self._playback_generation:
                continue
            self._chunk_started_at = None
            await self._send(ws, {"action": "playback.chunk.completed", **meta})
            self._chunk_active = False
            if self._pending_finish is not None and self._play_queue.empty():
                scope, self._pending_finish = self._pending_finish, None
                await self._report_finished(ws, scope)

    async def _finish_playback(self, ws: Any, scope: dict[str, str]) -> None:
        assert self._play_queue is not None
        if self._play_queue.empty() and not self._chunk_active:
            await self._report_finished(ws, scope)
        else:
            self._pending_finish = scope

    async def _report_finished(self, ws: Any, scope: dict[str, str]) -> None:
        self.is_playing = False
        self._scope = None
        self._playing_scope = None
        await self._send(ws, {"action": "playback.completed", **scope})
        self._emit("playback.completed", scope)
        self._on_state("idle")

    async def _interrupt_playback(self, ws: Any, data: Any) -> None:
        scope = _scope_of(data)
        if scope and self._scope and scope != self._scope:
            await self._send(ws, {"action": "playback.stopped", **scope, "played_audio_ms": 0})
            return
        # The public API wants only the current unacknowledged chunk's duration.
        played_ms = 0.0
        if self._chunk_started_at is not None:
            elapsed = asyncio.get_running_loop().time() - self._chunk_started_at
            played_ms = min(elapsed, self._chunk_duration) * 1000
        self._reset_playback(clear_context=False)
        self._interrupted_scope = scope
        self._on_flush()
        await self._send(
            ws,
            {"action": "playback.stopped", **scope, "played_audio_ms": max(0, int(played_ms))},
        )
        self._on_state("idle")

    def _reset_playback(self, *, clear_context: bool = True) -> None:
        self._playback_generation += 1
        self._playback_interrupted.set()
        if clear_context:
            self._chunk_meta.clear()
        if self._play_queue is not None:
            while not self._play_queue.empty():
                self._play_queue.get_nowait()
        self._scope = None
        self._playing_scope = None
        self._pending_finish = None
        self._chunk_active = False
        self._chunk_started_at = None
        self._chunk_duration = 0.0
        self._interrupted_scope = None
        self.is_playing = False

    def _emit(self, action: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            try:
                self._on_event(action, dict(data))
            except Exception:
                logger.exception("Conversation event observer failed; continuing audio")


def _scope_of(data: Any) -> dict[str, str]:
    """Extract the run and turn IDs used by playback receipts."""
    if not isinstance(data, dict):
        return {}
    run_id, turn_id = data.get("run_id"), data.get("turn_id")
    if isinstance(run_id, str) and isinstance(turn_id, str) and run_id and turn_id:
        return {"run_id": run_id, "turn_id": turn_id}
    return {}


def _chunk_of(data: Any) -> dict[str, str] | None:
    scope = _scope_of(data)
    chunk_id = data.get("chunk_id") if isinstance(data, dict) else None
    if not scope or not isinstance(chunk_id, str) or not chunk_id:
        return None
    return {"chunk_id": chunk_id, **scope}
