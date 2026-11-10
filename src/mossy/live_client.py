"""Realtime media client for https://platform.moss.cn/docs/reference/moss-live/.

Authenticate in the WebSocket handshake, attach and configure a session, then
stream tagged 16 kHz PCM and optional camera JPEGs on one socket. Downlink audio
is untagged PCM at the rate the server confirms: 48 kHz unless a lower rate was
requested. Tagging stays enabled when the camera is turned off.
"""

import asyncio
import concurrent.futures
import json
import logging
import ssl
import time
import urllib.request
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import websockets

logger = logging.getLogger(__name__)

MIC_SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 48000  # The downlink rate when none is requested.
_RECONNECT_DELAYS = (0.0, 1.0, 2.0, 5.0, 5.0)
_MIC_QUEUE_MAX = 200  # About four seconds of 20 ms microphone chunks.
_CAMERA_MAX_AGE = 3.0
_DEFAULT_OUTPUT = {"volume_db": 0.0, "speed": 1.0}
_REQUEST_TIMEOUT = 5.0
# Replies to configuration requests. Once a session is ready they answer a
# change made while talking, or repeat what a resumed session already had.
_CONFIRMATIONS = (
    "voice_changed",
    "session.updated",
    "media.configured",
    "media.output.configured",
    "client_context.updated",
)


class MossLive:
    """One conversation, driven by ``run`` on its own event loop.

    ``submit_*``, ``set_camera_enabled``, ``stats`` and the methods that change
    the conversation are called from other threads.
    """

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
        output_rate: int = TTS_SAMPLE_RATE,
        speed: float = 1.0,
        volume_db: float = 0.0,
        time_zone: str = "",
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
        self._persona = _persona(name, prompt)
        self._instructions = instructions
        self._camera_enabled = camera_enabled
        self._output_rate = output_rate
        self._output = {"volume_db": float(volume_db), "speed": float(speed)}
        self._time_zone = time_zone
        self._on_audio = on_audio
        self._on_flush = on_flush
        self._on_state = on_state
        self._on_text = on_text
        self._on_event = on_event

        self.session_id: str | None = None
        self.voice_id: str | None = None
        self.connected = False
        self.is_playing = False
        self.last_error: str | None = None
        self.output_rate = TTS_SAMPLE_RATE  # What the server confirmed for this connection.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: Any = None
        self._mic: asyncio.Queue[bytes] | None = None
        self._camera_frame: tuple[bytes, float] | None = None
        self._camera_wake: asyncio.Event | None = None
        self._camera_announced: bool | None = None
        self._camera_generation = 0
        self._camera_frames_sent = 0
        self._camera_sent_at: float | None = None
        self._send_lock = asyncio.Lock()
        self._session_sent = False
        # A server that predates media.configure keeps its 48 kHz default.
        self._media_configure = True
        self._awaiting_media = False
        self._waiting: deque[tuple[str, asyncio.Future[dict[str, Any]]]] = deque()
        self._chunk_meta: deque[dict[str, str]] = deque()
        # Chunks handed to the speaker and not yet acknowledged, with the loop
        # times at which each starts and finishes playing.
        self._playing: deque[tuple[dict[str, str], float, float]] = deque()
        self._play_cursor = 0.0
        self._play_wake = asyncio.Event()
        self._scope: dict[str, str] | None = None
        self._playing_scope: dict[str, str] | None = None
        self._pending_finish: dict[str, str] | None = None
        self._interrupted_scope: dict[str, str] | None = None
        self._playback_generation = 0

        self._reply_ms: deque[float] = deque(maxlen=50)
        self._reply_anchor: float | None = None
        self._speech_events = False
        self._dialed_at = 0.0
        self._handshake_ms: int | None = None
        self._ready_at: float | None = None
        self._counts = {"replies": 0, "reconnects": 0, "late_chunks": 0, "mic_dropped": 0}
        self._late_ms_max = 0.0

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
            self._counts["mic_dropped"] += 1
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

    # --- changes made while talking -----------------------------------------

    def change_voice(self, voice_id: str) -> None:
        """Use another voice from the next reply on, once the server confirms it."""
        voice_id = voice_id.strip()
        if not voice_id:
            # No action returns a session to the default voice; start a new one.
            self._voice_id = ""
            self.reconnect()
            return
        if self.connected:
            reply = self._request({"action": "change_voice", "voice_name": voice_id})
            if reply.get("name") != voice_id:
                raise RuntimeError(
                    f"Requested voice {voice_id}, server confirmed {reply.get('name')}"
                )
        self._voice_id = voice_id

    def update_session(self, name: str, prompt: str, instructions: str) -> None:
        persona = _persona(name, prompt)
        if self.connected:
            session = {"persona": persona, "instructions": instructions}
            reply = self._request({"action": "session.update", "session": session})
            if reply.get("error"):
                raise RuntimeError(f"Session update rejected: {reply['error']}")
        self._persona, self._instructions = persona, instructions

    def configure_output(self, volume_db: float, speed: float) -> None:
        output = {"volume_db": float(volume_db), "speed": float(speed)}
        if self.connected:
            self._request({"action": "media.output.configure", **output})
        self._output = output

    def update_time_zone(self, time_zone: str) -> None:
        if self.connected:
            reply = self._request(_client_context(time_zone))
            if not reply.get("accepted", True):
                raise RuntimeError(f"Time zone rejected: {reply.get('error')}")
        self._time_zone = time_zone

    def reconnect(self, **connection: Any) -> None:
        """Open a new socket and session, applying changed connection settings.

        Accepts ``base_url``, ``api_key``, ``model``, ``user``, ``verify_tls``
        and ``output_rate``.
        """
        for key, value in connection.items():
            if key not in ("base_url", "api_key", "model", "user", "verify_tls", "output_rate"):
                raise TypeError(f"Unknown connection setting: {key}")
            setattr(self, "_" + key, value.rstrip("/") if key == "base_url" else value)
        self.session_id = None  # A session cannot be resumed with other options.
        self._media_configure = True
        loop, ws = self._loop, self._ws
        if loop is not None and ws is not None:
            asyncio.run_coroutine_threadsafe(ws.close(), loop)

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one configuration action and wait for the server to confirm it."""
        loop = self._loop
        if loop is None:
            raise ConnectionError("MOSS-Live is not connected")
        future = asyncio.run_coroutine_threadsafe(self._confirmed(payload), loop)
        try:
            return future.result(_REQUEST_TIMEOUT)
        except (TimeoutError, concurrent.futures.TimeoutError):
            future.cancel()
            raise TimeoutError(f"MOSS-Live did not confirm {payload['action']}") from None

    async def _confirmed(self, payload: dict[str, Any]) -> dict[str, Any]:
        ws = self._ws
        if ws is None or not self.connected:
            raise ConnectionError("MOSS-Live is not connected")
        confirmation = {
            "change_voice": "voice_changed",
            "session.update": "session.updated",
            "media.output.configure": "media.output.configured",
            "client_context.update": "client_context.updated",
        }[payload["action"]]
        waiter = (confirmation, asyncio.get_running_loop().create_future())
        self._waiting.append(waiter)
        try:
            await self._send(ws, payload)
            return await waiter[1]
        finally:
            if waiter in self._waiting:
                self._waiting.remove(waiter)

    def stats(self) -> dict[str, Any]:
        """Client-side measurements for the status page.

        ``reply_ms`` runs from the end of the user's speech, as reported by the
        server, to the first reply audio reaching this client. It includes the
        network both ways and excludes the speaker's own output latency.
        """
        replies = sorted(self._reply_ms)
        ws = self._ws
        return {
            "reply_ms": round(self._reply_ms[-1]) if replies else None,
            "reply_ms_p50": round(replies[len(replies) // 2]) if replies else None,
            "reply_ms_p95": round(replies[int(len(replies) * 0.95)]) if replies else None,
            "reply_anchor": "speech_end" if self._speech_events else "transcript",
            "rtt_ms": round(ws.latency * 1000) if ws is not None and ws.latency else None,
            "handshake_ms": self._handshake_ms,
            "connected_s": (
                round(time.monotonic() - self._ready_at) if self._ready_at is not None else None
            ),
            "late_ms_max": round(self._late_ms_max),
            "output_rate": self.output_rate,
            **self._counts,
        }

    # --- connection ---------------------------------------------------------

    async def run(self, stop: asyncio.Event) -> None:
        """Keep the session up until stopped, clearing playback on disconnect."""
        self._loop = asyncio.get_running_loop()
        self._mic = asyncio.Queue()
        self._camera_wake = asyncio.Event()
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
                ready = asyncio.Event()
                try:
                    await self._session(stop, ready)
                except asyncio.CancelledError:
                    raise
                except websockets.ConnectionClosedOK as exc:
                    # The server rotates connections on a schedule; the session resumes.
                    logger.info("MOSS-Live closed the connection (%s); reconnecting", exc)
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
                    self._ready_at = None
                    self._reset_camera()
                    self._emit("session.disconnected", {})
                    self._on_flush()
                    self._reset_playback()
                    self._on_state("disconnected")
                # Retry at once after a session that worked, then back off.
                attempt = 0 if ready.is_set() else attempt + 1
                if ready.is_set() and not stop.is_set():
                    self._counts["reconnects"] += 1
        finally:
            self._loop = None

    async def _session(self, stop: asyncio.Event, ready: asyncio.Event) -> None:
        self._dialed_at = time.monotonic()
        async with websockets.connect(
            self._ws_url(),
            additional_headers={"Authorization": f"Bearer {self._api_key}"},
            ssl=self._ssl_context(),
            max_size=None,
            open_timeout=15,
            # A dead link is noticed within 15 s instead of the library's 40 s,
            # and a socket that cannot say goodbye does not delay the next one.
            ping_interval=5,
            ping_timeout=10,
            close_timeout=2,
            proxy=None,
        ) as ws:
            self.voice_id = None
            self.output_rate = TTS_SAMPLE_RATE
            self._session_sent = False
            self._awaiting_media = False
            self._reset_playback()
            self._reset_camera()
            self._drain_mic()
            attach: dict[str, Any] = {"action": "attach_session"}
            if self.session_id:
                attach["session_id"] = self.session_id
            await self._send(ws, attach)
            self._ws = ws
            tasks = [
                asyncio.create_task(self._receive(ws, ready)),
                asyncio.create_task(self._forward_mic(ws, ready)),
                asyncio.create_task(self._forward_camera(ws, ready)),
                asyncio.create_task(self._acknowledge(ws)),
                asyncio.create_task(stop.wait()),
            ]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                self._ws = None
                if self._awaiting_media and not ready.is_set():
                    logger.warning(
                        "MOSS-Live did not confirm %d Hz output; using its 48 kHz default",
                        self._output_rate,
                    )
                    self._media_configure = False
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                for _, waiter in self._waiting:
                    if not waiter.done():
                        waiter.set_exception(ConnectionError("MOSS-Live disconnected"))
                self._waiting.clear()

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
        return _tls_context(self._verify_tls)

    async def _send(self, ws: Any, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await ws.send(json.dumps(payload))

    async def _configure_session(self, ws: Any) -> None:
        if self._session_sent:
            return
        self._session_sent = True
        await self._send(
            ws,
            {
                "action": "session.update",
                "session": {"persona": self._persona, "instructions": self._instructions},
            },
        )

    async def _enable(self, ws: Any, ready: asyncio.Event) -> None:
        """Finish the handshake: choose the media framing and open the microphone."""
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
        self._ready_at = time.monotonic()
        self._handshake_ms = round((self._ready_at - self._dialed_at) * 1000)
        logger.info(
            "MOSS-Live session configured in %d ms; microphone enabled, %d Hz output",
            self._handshake_ms,
            self.output_rate,
        )
        # A rejection of these leaves the session usable, so they do not gate it.
        # The server forgets the output settings on every new connection.
        if self._time_zone:
            await self._send(ws, _client_context(self._time_zone))
        if self._output != _DEFAULT_OUTPUT:
            await self._send(ws, {"action": "media.output.configure", **self._output})

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
        loop = asyncio.get_running_loop()
        async for message in ws:
            if isinstance(message, bytes):
                meta = self._chunk_meta.popleft() if self._chunk_meta else None
                if meta is None:
                    raise RuntimeError("Audio arrived without response.audio.context")
                scope = _scope_of(meta)
                if scope == self._interrupted_scope:
                    continue
                now = loop.time()
                if scope != self._scope:
                    self._counts["replies"] += 1
                    if self._reply_anchor is not None:
                        self._reply_ms.append((time.monotonic() - self._reply_anchor) * 1000)
                        self._reply_anchor = None
                elif now > self._play_cursor:
                    # The speaker ran dry in the middle of a reply.
                    self._counts["late_chunks"] += 1
                    self._late_ms_max = max(self._late_ms_max, (now - self._play_cursor) * 1000)
                self._scope = scope
                self.is_playing = True
                self._on_state("speaking")
                # Hand audio over as it arrives. The audio device then plays it
                # back to back on its own clock; holding each chunk until the
                # previous one should have finished leaves a gap at every chunk
                # boundary, because the wait always overruns a little.
                if message:
                    self._on_audio(message)
                start = max(self._play_cursor, now)
                self._play_cursor = start + len(message) / 2 / self.output_rate
                self._playing.append((meta, start, self._play_cursor))
                self._play_wake.set()
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
        elif action in _CONFIRMATIONS and ready.is_set():
            if action == "voice_changed":
                self.voice_id = data.get("name")
            waiter = next((w for w in self._waiting if w[0] == action), None)
            if waiter is not None:
                self._waiting.remove(waiter)
                if not waiter[1].done():
                    waiter[1].set_result(data)
            elif data.get("error"):
                logger.warning("MOSS-Live rejected %s: %s", action, data["error"])
        elif action == "voice_changed":
            voice_id = data.get("name")
            if self._voice_id and voice_id != self._voice_id:
                # A resumed session may be tied to another voice; start a new one.
                self.session_id = None
                raise RuntimeError(f"Requested voice {self._voice_id}, server confirmed {voice_id}")
            self.voice_id = voice_id
            logger.info("MOSS-Live voice confirmed: %s", voice_id)
            if self._voice_id:
                await self._configure_session(ws)
        elif action == "session.updated":
            if data.get("error"):
                raise RuntimeError(f"Session configuration failed: {data['error']}")
            if self._voice_id and self.voice_id != self._voice_id:
                raise RuntimeError("Session configured before the requested voice was confirmed")
            if self._output_rate == TTS_SAMPLE_RATE or not self._media_configure:
                await self._enable(ws, ready)
            elif not self._awaiting_media:
                self._awaiting_media = True
                output = {"format": "pcm16", "sample_rate": self._output_rate}
                await self._send(ws, {"action": "media.configure", "output_audio": output})
        elif action == "media.configured" and self._awaiting_media:
            self._awaiting_media = False
            output = data.get("output_audio") or {}
            if data.get("error"):
                logger.warning(
                    "MOSS-Live refused %d Hz output (%s); using its 48 kHz default",
                    self._output_rate,
                    data["error"],
                )
            elif output.get("format", "pcm16") != "pcm16":
                raise RuntimeError(f"Unsupported downlink audio format: {output.get('format')}")
            else:
                self.output_rate = int(output.get("sample_rate") or self._output_rate)
            await self._enable(ws, ready)
        elif action == "input.speech.started":
            self._speech_events = True
            self._reply_anchor = None
            self._on_state("listening")
        elif action == "input.speech.stopped":
            self._speech_events = True
            self._reply_anchor = time.monotonic()
        elif action in ("input.transcript.updated", "input.transcript.done"):
            if action.endswith(".done") and not self._speech_events:
                self._reply_anchor = time.monotonic()
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
        elif action == "session.closing":
            logger.info("MOSS-Live is closing this connection: %s", data.get("reason"))
        elif action == "error":
            message = f"MOSS-Live error: {data.get('error', data)}"
            if self._awaiting_media:
                logger.warning("%s; using the 48 kHz default output", message)
                self._awaiting_media = False
                self._media_configure = False
                await self._enable(ws, ready)
            elif not ready.is_set():
                raise RuntimeError(message)
            elif self._waiting:
                # Errors do not name the request they answer; it is the oldest.
                _, waiter = self._waiting.popleft()
                if not waiter.done():
                    waiter.set_exception(RuntimeError(message))
            else:
                # The API reference keeps the session open after a rejected request.
                logger.warning(message)
        else:
            logger.debug("Unhandled MOSS-Live action: %s", action)

    # --- playback -----------------------------------------------------------

    async def _acknowledge(self, ws: Any) -> None:
        """Report playback progress on the audio clock.

        The audio is already queued in the speaker. Reachy's SDK exposes
        enqueue, not a hardware completion callback, so this clock estimates
        completion from PCM duration; it does not measure the device's output
        latency. Every deadline is absolute, so a late wake-up does not delay
        the receipts that follow it.
        """
        loop = asyncio.get_running_loop()
        while True:
            self._play_wake.clear()
            timeout = None
            if self._playing:
                meta, start, end = self._playing[0]
                scope = _scope_of(meta)
                now = loop.time()
                if scope != self._playing_scope and now >= start:
                    self._playing_scope = scope
                    self._emit("playback.started", scope)
                if scope == self._playing_scope and now >= end:
                    self._playing.popleft()
                    generation = self._playback_generation
                    await self._send(ws, {"action": "playback.chunk.completed", **meta})
                    if (
                        generation == self._playback_generation
                        and self._pending_finish is not None
                        and not self._playing
                    ):
                        scope, self._pending_finish = self._pending_finish, None
                        await self._report_finished(ws, scope)
                    continue
                timeout = (end if scope == self._playing_scope else start) - now
            try:
                await asyncio.wait_for(self._play_wake.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass

    async def _finish_playback(self, ws: Any, scope: dict[str, str]) -> None:
        if self._playing:
            self._pending_finish = scope
        else:
            await self._report_finished(ws, scope)

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
        # The public API wants chunks that finished confirmed first, then the
        # played part of the chunk that was cut short.
        now = asyncio.get_running_loop().time()
        finished = [meta for meta, _, end in self._playing if end <= now]
        played_ms = 0.0
        if len(finished) < len(self._playing):
            played_ms = (now - self._playing[len(finished)][1]) * 1000
        self._reset_playback(clear_context=False)
        self._interrupted_scope = scope
        self._on_flush()
        for meta in finished:
            await self._send(ws, {"action": "playback.chunk.completed", **meta})
        await self._send(
            ws,
            {"action": "playback.stopped", **scope, "played_audio_ms": max(0, int(played_ms))},
        )
        self._on_state("idle")

    def _reset_playback(self, *, clear_context: bool = True) -> None:
        self._playback_generation += 1
        if clear_context:
            self._chunk_meta.clear()
        self._playing.clear()
        self._play_cursor = 0.0
        self._play_wake.set()
        self._scope = None
        self._playing_scope = None
        self._pending_finish = None
        self._interrupted_scope = None
        self.is_playing = False

    def _emit(self, action: str, data: dict[str, Any]) -> None:
        if self._on_event is not None:
            try:
                self._on_event(action, dict(data))
            except Exception:
                logger.exception("Conversation event observer failed; continuing audio")


def list_voices(base_url: str, api_key: str, verify_tls: bool = True) -> list[dict[str, Any]]:
    """The account's voices, whose ``id`` is what ``change_voice`` expects."""
    handlers: list[Any] = [urllib.request.ProxyHandler({})]  # Direct, like the socket.
    if base_url.startswith("https://"):
        handlers.append(urllib.request.HTTPSHandler(context=_tls_context(verify_tls)))
    opener = urllib.request.build_opener(*handlers)
    voices: list[dict[str, Any]] = []
    query = {"limit": "150"}
    for _ in range(10):  # 1500 voices are plenty for a menu.
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/v1/audio/voices?{urlencode(query)}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with opener.open(request, timeout=10) as response:
            page = json.load(response)
        voices += [
            {
                "id": voice["id"],
                "name": voice.get("name") or "",
                "created_at": voice.get("created_at"),
            }
            for voice in page.get("data") or []
            if isinstance(voice, dict) and voice.get("id")
        ]
        if not page.get("has_more") or not page.get("next_cursor"):
            break
        query["after"] = page["next_cursor"]
    return voices


def _tls_context(verify: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _persona(name: str, prompt: str) -> dict[str, str]:
    """Setting only the name keeps the server's default persona prompt."""
    return {"name": name, "prompt": prompt} if prompt else {"name": name}


def _client_context(time_zone: str) -> dict[str, Any]:
    context = {"time_zone": time_zone} if time_zone else None
    return {"action": "client_context.update", "context": context}


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
