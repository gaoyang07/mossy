"""Client for the MOSS-Live realtime voice websocket API.

MOSS-Live does VAD, ASR, the LLM turn and TTS server-side, so this client only
moves audio: microphone in, speech out. The protocol below was read off the
browser bundle the deployment serves at ``{base}/xtalk/index.js``:

1. ``POST {base}/api/auth/login`` (no body) -> ``{"access_token": ..., "user": ...}``
2. connect ``wss://{host}/ws?access_token=<jwt>``
3. send ``attach_session`` and wait for the ``session_attached`` reply
4. stream the microphone as raw binary frames: mono int16 LE at 16 kHz
5. receive text frames shaped ``{"action": ..., "data": ...}`` and binary frames
   carrying TTS audio as mono int16 LE at 48 kHz; every binary frame is preceded
   by a ``tts_chunk_context`` naming the chunk it belongs to
6. acknowledge playback with ``tts_chunk_started`` / ``tts_chunk_played`` /
   ``tts_playback_finished`` / ``tts_playback_interrupted`` — the server tracks
   these to know when the client stopped talking, so they cannot be skipped

Binary frames are sent untagged. The server also understands a tagged framing
(one leading byte: 0 audio, 1 camera, 2 screen) enabled with a ``media.mode``
message, but that is only needed to multiplex video.
"""

import asyncio
import json
import logging
import ssl
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx
import websockets

logger = logging.getLogger(__name__)

# Wire formats fixed by the server.
MIC_SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 48000

# Same backoff ladder the browser client uses between reconnect attempts.
_RECONNECT_DELAYS = (0.0, 1.0, 2.0, 5.0, 5.0)

# Roughly 4 s of 20 ms microphone chunks. If the uplink stalls, keeping the
# newest audio and dropping the rest beats replaying a backlog nobody wants.
_MIC_QUEUE_MAX = 200


class MossLive:
    """A single long-lived MOSS-Live conversation.

    ``run()`` owns an asyncio loop; ``submit_mic()`` is the only method meant to
    be called from another thread.
    """

    def __init__(
        self,
        base_url: str,
        *,
        verify_tls: bool = False,
        persona: str = "",
        voice: str = "",
        on_audio: Callable[[bytes], None],
        on_flush: Callable[[], None],
        on_state: Callable[[str], None],
        on_text: Callable[[str, str, bool], None],
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._verify_tls = verify_tls
        self._persona = persona
        self._voice = voice
        self._on_audio = on_audio
        self._on_flush = on_flush
        self._on_state = on_state
        self._on_text = on_text

        self.session_id: str | None = None
        self.connected = False
        self.is_playing = False
        self.latency: dict[str, int] = {}

        self._loop: asyncio.AbstractEventLoop | None = None
        self._mic: asyncio.Queue[bytes] | None = None
        self._send_lock = asyncio.Lock()

        # Playback bookkeeping for the acknowledgement protocol.
        self._chunk_meta: deque[dict[str, str]] = deque()
        self._play_queue: asyncio.Queue[tuple[dict[str, str] | None, float]] | None = None
        self._pacer: asyncio.Task[None] | None = None
        self._scope: dict[str, str] | None = None
        self._pending_finish: dict[str, str] | None = None
        self._played_ms = 0.0
        self._chunk_active = False
        self._chunk_started_at: float | None = None
        self._chunk_duration = 0.0
        self._discard_audio = False
        self._resumed = asyncio.Event()
        self._resumed.set()

    def submit_mic(self, pcm: bytes) -> None:
        """Queue one microphone chunk (mono int16 LE at 16 kHz).

        Safe to call from a thread other than the one running ``run()``. Chunks
        submitted before the session is up are dropped.
        """
        loop = self._loop
        if loop is None or self._mic is None:
            return
        loop.call_soon_threadsafe(self._enqueue_mic, pcm)

    def _enqueue_mic(self, pcm: bytes) -> None:
        assert self._mic is not None
        while self._mic.qsize() >= _MIC_QUEUE_MAX:
            self._mic.get_nowait()
        self._mic.put_nowait(pcm)

    async def run(self, stop: asyncio.Event) -> None:
        """Keep a session up until ``stop`` is set, reconnecting on failure."""
        self._loop = asyncio.get_running_loop()
        self._mic = asyncio.Queue()
        self._play_queue = asyncio.Queue()

        attempt = 0
        while not stop.is_set():
            delay = _RECONNECT_DELAYS[min(attempt, len(_RECONNECT_DELAYS) - 1)]
            if delay:
                logger.info("Reconnecting to MOSS-Live in %.0fs", delay)
                await asyncio.sleep(delay)
            attached = False
            try:
                attached = await self._session(stop)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("MOSS-Live session ended: %s: %s", type(e).__name__, e)
            finally:
                self.connected = False
                self._on_state("disconnected")
            attempt = 0 if attached else attempt + 1

    async def _session(self, stop: asyncio.Event) -> bool:
        """Run one websocket session. Returns whether it reached ``session_attached``."""
        token = await self._login()
        url = f"{self._ws_url()}?access_token={token}"

        async with websockets.connect(
            url,
            ssl=self._ssl_context(),
            max_size=None,
            open_timeout=15,
            ping_interval=20,
            proxy=None,  # see _login()
        ) as ws:
            self._reset_playback()
            self._drain_mic()
            await self._send(
                ws,
                {
                    "action": "attach_session",
                    "permission_profile": "read_only",
                    "session_id": self.session_id,
                },
            )

            attached = asyncio.Event()
            tasks = [
                asyncio.create_task(self._receive(ws, attached)),
                asyncio.create_task(self._forward_mic(ws)),
                asyncio.create_task(stop.wait()),
            ]
            self._pacer = asyncio.create_task(self._pace_playback(ws))
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self._cancel_pacer()
            for task in done:
                task.result()  # re-raise whatever ended the session
            return attached.is_set()

    # --- transport ----------------------------------------------------------

    def _ws_url(self) -> str:
        if self._base_url.startswith("https://"):
            return "wss://" + self._base_url[len("https://") :] + "/ws"
        if self._base_url.startswith("http://"):
            return "ws://" + self._base_url[len("http://") :] + "/ws"
        raise ValueError(f"MOSS_LIVE_URL must be http(s)://, got {self._base_url!r}")

    def _ssl_context(self) -> ssl.SSLContext | None:
        if not self._base_url.startswith("https://"):
            return None
        context = ssl.create_default_context()
        if not self._verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    async def _login(self) -> str:
        # trust_env=False, and proxy=None on the socket: a realtime audio stream
        # has no business being tunnelled through whatever HTTPS_PROXY/ALL_PROXY
        # happens to be exported in the shell that launched the robot.
        async with httpx.AsyncClient(
            verify=self._verify_tls, timeout=20.0, trust_env=False
        ) as client:
            resp = await client.post(f"{self._base_url}/api/auth/login")
            resp.raise_for_status()
            token = resp.json().get("access_token")
        if not token:
            raise RuntimeError("MOSS-Live login response did not include access_token")
        return str(token)

    async def _send(self, ws: Any, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await ws.send(json.dumps(payload))

    async def _forward_mic(self, ws: Any) -> None:
        assert self._mic is not None
        while True:
            chunk = await self._mic.get()
            async with self._send_lock:
                await ws.send(chunk)

    def _drain_mic(self) -> None:
        assert self._mic is not None
        while not self._mic.empty():
            self._mic.get_nowait()

    # --- receive ------------------------------------------------------------

    async def _receive(self, ws: Any, attached: asyncio.Event) -> None:
        assert self._play_queue is not None
        async for message in ws:
            if isinstance(message, bytes):
                meta = self._chunk_meta.popleft() if self._chunk_meta else None
                if self._discard_audio:
                    # Audio of an interrupted turn, still in flight when the
                    # server told us to stop. Playing it would talk over the
                    # user who just interrupted.
                    logger.debug("Discarding %d bytes from an interrupted turn", len(message))
                    continue
                self._on_audio(message)
                duration = len(message) / 2 / TTS_SAMPLE_RATE
                self._play_queue.put_nowait((meta, duration))
                continue
            try:
                frame = json.loads(message)
            except ValueError:
                logger.warning("Ignoring non-JSON text frame from MOSS-Live")
                continue
            await self._handle(ws, frame.get("action"), frame.get("data"), attached)

    async def _handle(
        self, ws: Any, action: str | None, data: Any, attached: asyncio.Event
    ) -> None:
        if not isinstance(data, dict):
            data = {}

        if action == "session_attached":
            self.session_id = data.get("session_id")
            self.connected = True
            attached.set()
            logger.info("Attached to MOSS-Live session %s", self.session_id)
            self._on_state("idle")
            if self._persona:
                await self._send(ws, {"action": "change_persona", "persona_id": self._persona})
            if self._voice:
                await self._send(ws, {"action": "change_voice", "voice_name": self._voice})

        elif action == "vad_speech_start":
            self._on_state("listening")
        elif action == "vad_speech_end":
            self._on_state("idle")

        elif action in ("update_asr", "finish_asr"):
            self._on_text("user", data.get("text", ""), action == "finish_asr")
        elif action in ("update_resp", "finish_resp", "response_interrupted"):
            self._on_text("assistant", data.get("text", ""), action != "update_resp")

        elif action == "start_tts":
            self._scope = _scope_of(data)
            self._played_ms = 0.0
            self._discard_audio = False
            self.is_playing = True
            self._on_state("speaking")
        elif action == "tts_chunk_context":
            meta = _chunk_of(data)
            if meta is not None:
                self._chunk_meta.append(meta)
        elif action == "tts_finished":
            await self._finish_playback(ws, _scope_of(data))
        elif action == "stop_tts":
            await self._interrupt_playback(ws, data)
        elif action == "pause_tts":
            self._resumed.clear()
            logger.warning(
                "MOSS-Live asked to pause playback; audio already queued on the "
                "robot will finish playing (the record and playback GStreamer "
                "pipelines share a state, so they cannot be paused separately)."
            )
        elif action == "resume_tts":
            self._resumed.set()

        elif action == "latency_metrics":
            self.latency = {
                "asr": int(data.get("asr_latency_ms") or 0),
                "llm_first_token": int(data.get("llm_first_token_ms") or 0),
                "llm_sentence": int(data.get("llm_sentence_ms") or 0),
                "tts_first_chunk": int(data.get("tts_first_chunk_ms") or 0),
            }
        else:
            logger.debug("Unhandled MOSS-Live action: %s", action)

    # --- playback acknowledgement -------------------------------------------

    async def _pace_playback(self, ws: Any) -> None:
        """Acknowledge each audio chunk at the wall-clock time it is heard.

        Chunks are handed to the speaker as they arrive — faster than real time —
        so the acknowledgements the server waits on are paced here instead, off
        the chunk durations.
        """
        assert self._play_queue is not None
        loop = asyncio.get_running_loop()
        while True:
            meta, duration = await self._play_queue.get()
            # Claimed before the first await so that a `tts_finished` arriving
            # right now does not see an empty queue and ack playback early.
            self._chunk_active = True
            await self._resumed.wait()
            await self._send(ws, {"action": "tts_chunk_started", **(meta or {})})
            self._chunk_started_at = loop.time()
            self._chunk_duration = duration
            await asyncio.sleep(duration)
            self._chunk_started_at = None
            self._played_ms += duration * 1000
            await self._send(ws, {"action": "tts_chunk_played", **(meta or {})})
            self._chunk_active = False
            if self._pending_finish is not None and self._play_queue.empty():
                scope, self._pending_finish = self._pending_finish, None
                await self._report_finished(ws, scope)

    async def _finish_playback(self, ws: Any, scope: dict[str, str]) -> None:
        """Server says the turn's audio is fully sent; ack once it has played."""
        assert self._play_queue is not None
        if self._play_queue.empty() and not self._chunk_active:
            await self._report_finished(ws, scope)
        else:
            self._pending_finish = scope

    async def _report_finished(self, ws: Any, scope: dict[str, str]) -> None:
        self.is_playing = False
        self._scope = None
        await self._send(ws, {"action": "tts_playback_finished", **scope})
        self._on_state("idle")

    async def _interrupt_playback(self, ws: Any, data: Any) -> None:
        """Barge-in: drop everything queued and report how much was actually heard."""
        scope = _scope_of(data)
        if scope and self._scope and scope != self._scope:
            # A stale turn we are no longer playing; nothing was heard from it.
            await self._send(
                ws,
                {
                    "action": "tts_playback_interrupted",
                    "reason": _reason_of(data),
                    **scope,
                    "played_audio_ms": 0,
                },
            )
            return

        played_ms = self._played_ms
        if self._chunk_started_at is not None:
            elapsed = asyncio.get_running_loop().time() - self._chunk_started_at
            played_ms += min(elapsed, self._chunk_duration) * 1000

        await self._cancel_pacer()
        self._reset_playback()
        self._discard_audio = True
        self._on_flush()

        payload: dict[str, Any] = {
            "action": "tts_playback_interrupted",
            "reason": _reason_of(data),
            "played_audio_ms": max(0, int(played_ms)),
        }
        payload.update(scope)
        await self._send(ws, payload)
        self._on_state("idle")
        self._pacer = asyncio.create_task(self._pace_playback(ws))

    async def _cancel_pacer(self) -> None:
        if self._pacer is None:
            return
        self._pacer.cancel()
        try:
            await self._pacer
        except asyncio.CancelledError:
            pass
        self._pacer = None

    def _reset_playback(self) -> None:
        self._chunk_meta.clear()
        if self._play_queue is not None:
            while not self._play_queue.empty():
                self._play_queue.get_nowait()
        self._scope = None
        self._pending_finish = None
        self._played_ms = 0.0
        self._chunk_active = False
        self._chunk_started_at = None
        self._chunk_duration = 0.0
        self._discard_audio = False
        self.is_playing = False
        self._resumed.set()


def _scope_of(data: Any) -> dict[str, str]:
    """Extract the ``run_id``/``turn_id`` pair, or ``{}`` for legacy turns."""
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


def _reason_of(data: Any) -> str:
    if isinstance(data, dict) and isinstance(data.get("reason"), str):
        return data["reason"]
    return ""
