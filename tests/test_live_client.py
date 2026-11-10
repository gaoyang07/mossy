"""Public-protocol integration tests with a local WebSocket peer."""

import asyncio
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve

from mossy.live_client import MossLive, list_voices


class LiveProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(
        self, scenario, *, on_event=None, camera_enabled=False, voice_id="", **options
    ):
        received_audio = []
        transcript = []
        flushed = []
        stop = asyncio.Event()
        result = asyncio.get_running_loop().create_future()
        client = None

        async def handler(ws):
            try:
                self.assertEqual(ws.request.headers["Authorization"], "Bearer test-only-key")
                parsed = urlsplit(ws.request.path)
                self.assertEqual(parsed.path, "/v1/realtime")
                self.assertNotIn("test-only-key", ws.request.path)
                self.assertEqual(parse_qs(parsed.query)["model"], ["moss-live-1.0"])
                self.assertEqual(json.loads(await ws.recv()), {"action": "attach_session"})
                client.submit_mic(b"early input must be dropped")
                await ws.send(
                    json.dumps({"action": "session_attached", "data": {"session_id": "test"}})
                )
                if voice_id:
                    self.assertEqual(
                        json.loads(await ws.recv()),
                        {"action": "change_voice", "voice_name": voice_id},
                    )
                    self.assertFalse(client.connected)
                    self.assertIsNone(client.voice_id)
                    client.submit_mic(b"input before voice confirmation must be dropped")
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), 0.02)
                    await ws.send(
                        json.dumps({"action": "voice_changed", "data": {"name": voice_id}})
                    )
                update = json.loads(await ws.recv())
                self.assertEqual(update["action"], "session.update")
                self.assertEqual(update["session"]["persona"]["name"], "MOSS")
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(ws.recv(), 0.02)
                await scenario(ws, client, received_audio, transcript, flushed)
                result.set_result(None)
            except Exception as exc:
                result.set_exception(exc)
            finally:
                stop.set()
                await ws.wait_closed()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = MossLive(
                f"http://127.0.0.1:{port}",
                api_key="test-only-key",
                voice_id=voice_id,
                camera_enabled=camera_enabled,
                on_audio=received_audio.append,
                on_flush=lambda: flushed.append(True),
                on_state=lambda state: None,
                on_text=lambda *args: transcript.append(args),
                on_event=on_event,
                **options,
            )
            task = asyncio.create_task(client.run(stop))
            try:
                await asyncio.wait_for(result, 3)
            finally:
                stop.set()
                await asyncio.wait_for(task, 2)
            self.assertFalse(client.connected)
            self.assertIsNone(client.voice_id)
            self.assertFalse(client.is_playing)
        return client

    async def assert_quiet(self, ws, seconds=0.03):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), seconds)

    async def speak(self, ws, scope, chunks):
        """Send reply audio as fast as the socket takes it; chunks are (id, pcm)."""
        for chunk_id, pcm in chunks:
            context = {"action": "response.audio.context", "data": {"chunk_id": chunk_id, **scope}}
            await ws.send(json.dumps(context))
            await ws.send(pcm)

    async def ready(self, ws, client, *, camera=False):
        await ws.send(json.dumps({"action": "session.updated", "data": {"error": ""}}))
        await self.opened(ws, client, camera=camera)

    async def opened(self, ws, client, *, camera=False, then=()):
        self.assertEqual(
            json.loads(await ws.recv()), {"action": "media.mode", "binary_protocol": "tagged-v1"}
        )
        self.assertEqual(
            json.loads(await ws.recv()),
            {"action": "video.state", "source": "camera", "enabled": camera},
        )
        if camera:
            self.assertEqual(
                json.loads(await ws.recv()), {"action": "video.stream_focus", "source": "camera"}
            )
        self.assertEqual(json.loads(await ws.recv()), {"action": "mic_state", "enabled": True})
        for frame in then:
            self.assertEqual(json.loads(await ws.recv()), frame)
        await asyncio.sleep(0)
        self.assertTrue(client.connected)
        client.submit_mic(bytes(640))
        self.assertEqual(await ws.recv(), b"\x00" + bytes(640))

    async def test_config_input_transcripts_and_playback_receipts(self):
        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client)
            scope = {"run_id": "run-1", "turn_id": "turn-1"}
            for action, text in [
                ("input.transcript.done", "你好"),
                ("output.transcript.done", "你好呀"),
            ]:
                await ws.send(json.dumps({"action": action, "data": {"text": text, **scope}}))
            pcm = bytes(1920)  # 20 ms at 48 kHz, int16.
            for chunk_id in ["chunk-1", "chunk-2"]:
                await ws.send(
                    json.dumps(
                        {
                            "action": "response.audio.context",
                            "data": {"chunk_id": chunk_id, **scope},
                        }
                    )
                )
                await ws.send(pcm)
            await ws.send(json.dumps({"action": "response.audio.done", "data": scope}))
            receipts = [json.loads(await ws.recv()) for _ in range(3)]
            self.assertEqual(
                [r["action"] for r in receipts],
                ["playback.chunk.completed", "playback.chunk.completed", "playback.completed"],
            )
            self.assertEqual([r.get("chunk_id") for r in receipts[:2]], ["chunk-1", "chunk-2"])
            self.assertTrue(
                all(r["run_id"] == "run-1" and r["turn_id"] == "turn-1" for r in receipts)
            )
            self.assertEqual(played, [pcm, pcm])
            self.assertEqual(transcript, [("user", "你好", True), ("assistant", "你好呀", True)])
            await ws.send(
                json.dumps({"action": "response.ended", "data": {"status": "completed", **scope}})
            )

        client = await self.exercise(scenario)
        self.assertIsNone(client.last_error)

    async def test_burst_audio_reaches_the_speaker_at_once_and_receipts_keep_playback_time(self):
        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client)
            scope = {"run_id": "run-1", "turn_id": "turn-1"}
            pcm = bytes(1920)  # 20 ms at 48 kHz, int16.
            count = 50  # One second of speech, sent faster than it can be spoken.
            started = time.monotonic()
            await self.speak(ws, scope, [(str(index), pcm) for index in range(count)])
            await ws.send(json.dumps({"action": "response.audio.done", "data": scope}))

            # The device paces playback. Holding chunks back until the previous
            # one has "finished" leaves a gap at every chunk boundary.
            first = json.loads(await ws.recv())
            self.assertEqual(first["chunk_id"], "0")
            self.assertEqual(len(played), count)

            # A busy event loop makes every timer fire late. Receipts follow the
            # audio clock, so that lateness must not add up chunk after chunk.
            # A chain of per-chunk waits takes about 1.7 s under these stalls.
            async def stall_the_loop():
                while True:
                    time.sleep(0.025)
                    await asyncio.sleep(0.005)

            busy = asyncio.create_task(stall_the_loop())
            receipts = [first] + [json.loads(await ws.recv()) for _ in range(count)]
            elapsed = time.monotonic() - started
            busy.cancel()
            self.assertEqual(
                [r.get("chunk_id") for r in receipts[:-1]], list(map(str, range(count)))
            )
            self.assertEqual(receipts[-1]["action"], "playback.completed")
            self.assertGreaterEqual(elapsed, 0.98)
            self.assertLess(elapsed, 1.3)

        await self.exercise(scenario)

    async def test_interruption_reports_only_unacknowledged_chunk(self):
        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client)
            scope = {"run_id": "run-1", "turn_id": "turn-1"}

            async def chunk(chunk_id, pcm, turn=scope):
                await ws.send(
                    json.dumps(
                        {"action": "response.audio.context", "data": {"chunk_id": chunk_id, **turn}}
                    )
                )
                await ws.send(pcm)

            await chunk("complete", bytes(4800))  # 50 ms, already acknowledged.
            self.assertEqual(json.loads(await ws.recv())["action"], "playback.chunk.completed")
            await chunk("interrupt-me", bytes(48000))
            while len(played) < 2:
                await asyncio.sleep(0.001)
            await asyncio.sleep(0.01)
            await ws.send(json.dumps({"action": "response.audio.stop", "data": scope}))
            receipt = json.loads(await ws.recv())
            self.assertEqual(receipt["action"], "playback.stopped")
            self.assertGreaterEqual(receipt["played_audio_ms"], 0)
            self.assertLess(receipt["played_audio_ms"], 50)
            self.assertTrue(flushed)
            await chunk("stale", bytes(1920))
            next_scope = {"run_id": "run-2", "turn_id": "turn-2"}
            await chunk("next", bytes(1920), next_scope)
            await ws.send(json.dumps({"action": "response.audio.done", "data": next_scope}))
            receipts = [json.loads(await ws.recv()), json.loads(await ws.recv())]
            self.assertEqual(receipts[0]["chunk_id"], "next")
            self.assertEqual(receipts[1]["action"], "playback.completed")
            self.assertEqual(len(played), 3)

        await self.exercise(scenario)

    async def test_configuration_error_does_not_enable_microphone(self):
        async def scenario(ws, client, played, transcript, flushed):
            await ws.send(
                json.dumps({"action": "session.updated", "data": {"error": "invalid persona"}})
            )
            await ws.wait_closed()
            self.assertFalse(client.connected)
            self.assertEqual(played, [])

        client = await self.exercise(scenario)
        self.assertIn("invalid persona", client.last_error)

    async def test_voice_is_confirmed_before_enabling_input(self):
        async def scenario(ws, client, played, transcript, flushed):
            self.assertEqual(client.voice_id, "test-cloned-voice")
            await self.ready(ws, client)

        client = await self.exercise(scenario, voice_id="test-cloned-voice")
        self.assertIsNone(client.last_error)

    async def test_incorrect_or_missing_voice_confirmation_keeps_input_disabled(self):
        for action, data in (
            ("voice_changed", {"name": "different-voice"}),
            ("voice_changed", {}),
            ("session.updated", {"error": ""}),
        ):
            with self.subTest(action=action, data=data):
                client = MossLive(
                    "http://127.0.0.1",
                    api_key="test-only-key",
                    voice_id="test-cloned-voice",
                    on_audio=lambda pcm: None,
                    on_flush=lambda: None,
                    on_state=lambda state: None,
                    on_text=lambda *args: None,
                )
                ws = AsyncMock()
                ready = asyncio.Event()
                with self.assertRaises(RuntimeError):
                    await client._handle(ws, action, data, ready)
                ws.send.assert_not_called()
                self.assertFalse(ready.is_set())
                self.assertFalse(client.connected)
                self.assertIsNone(client.voice_id)

    async def test_motion_events_preserve_scope_and_follow_local_playback(self):
        events = []
        scope = {"run_id": "motion-run", "turn_id": "motion-turn"}

        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client)
            await ws.send(
                json.dumps(
                    {"action": "output.transcript.updated", "data": {**scope, "text": "太好了！"}}
                )
            )
            for index in range(2):
                await ws.send(
                    json.dumps(
                        {
                            "action": "response.audio.context",
                            "data": {**scope, "chunk_id": str(index)},
                        }
                    )
                )
                await ws.send(bytes(1920))
            await ws.send(json.dumps({"action": "response.audio.done", "data": scope}))
            self.assertEqual(json.loads(await ws.recv())["action"], "playback.chunk.completed")
            self.assertEqual(json.loads(await ws.recv())["action"], "playback.chunk.completed")
            self.assertEqual(json.loads(await ws.recv())["action"], "playback.completed")
            self.assertEqual(len(played), 2)

        await self.exercise(scenario, on_event=lambda action, data: events.append((action, data)))
        starts = [data for action, data in events if action == "playback.started"]
        ends = [data for action, data in events if action == "playback.completed"]
        self.assertEqual(starts, [scope])
        self.assertEqual(ends, [scope])
        self.assertEqual(events[0], ("output.transcript.updated", {**scope, "text": "太好了！"}))
        self.assertEqual(events[-1], ("session.disconnected", {}))

    async def test_interrupted_text_stays_a_distinct_motion_event(self):
        events = []
        scope = {"run_id": "old-run", "turn_id": "old-turn"}

        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client)
            await ws.send(
                json.dumps(
                    {"action": "response.interrupted", "data": {**scope, "text": "太好了！"}}
                )
            )
            await ws.send(json.dumps({"action": "response.audio.stop", "data": scope}))
            self.assertEqual(json.loads(await ws.recv())["action"], "playback.stopped")

        await self.exercise(scenario, on_event=lambda action, data: events.append((action, data)))
        self.assertEqual(events[0][0], "response.interrupted")
        self.assertEqual(events[0][1]["turn_id"], "old-turn")
        self.assertNotIn("output.transcript.done", [action for action, _ in events])

    async def test_motion_observer_failure_does_not_interrupt_audio(self):
        def broken_observer(action, data):
            raise RuntimeError("test gesture failure")

        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client)
            scope = {"run_id": "run", "turn_id": "turn"}
            await ws.send(
                json.dumps(
                    {"action": "response.audio.context", "data": {**scope, "chunk_id": "one"}}
                )
            )
            await ws.send(bytes(1920))
            await ws.send(json.dumps({"action": "response.audio.done", "data": scope}))
            self.assertEqual(json.loads(await ws.recv())["action"], "playback.chunk.completed")
            self.assertEqual(json.loads(await ws.recv())["action"], "playback.completed")
            self.assertEqual(len(played), 1)

        with self.assertLogs("mossy.live_client", level="ERROR"):
            client = await self.exercise(scenario, on_event=broken_observer)
        self.assertIsNone(client.last_error)

    async def test_camera_jpeg_and_audio_tags_with_live_toggle(self):
        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client, camera=True)
            jpeg = b"\xff\xd8camera frame\xff\xd9"
            client.submit_camera(jpeg, time.monotonic())
            self.assertEqual(await ws.recv(), b"\x01" + jpeg)
            self.assertEqual(client.camera_status()["sent_frames"], 1)

            client.set_camera_enabled(False)
            self.assertEqual(
                json.loads(await ws.recv()),
                {"action": "video.state", "source": "camera", "enabled": False},
            )
            client.submit_camera(jpeg, time.monotonic())
            client.submit_mic(bytes(640))
            self.assertEqual(await ws.recv(), b"\x00" + bytes(640))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.02)

            client.set_camera_enabled(True)
            self.assertEqual(
                json.loads(await ws.recv()),
                {"action": "video.state", "source": "camera", "enabled": True},
            )
            self.assertEqual(
                json.loads(await ws.recv()), {"action": "video.stream_focus", "source": "camera"}
            )
            client.submit_camera(jpeg, time.monotonic())
            self.assertEqual(await ws.recv(), b"\x01" + jpeg)
            self.assertTrue(client.connected)

        await self.exercise(scenario, camera_enabled=True)

    async def test_camera_keeps_latest_frame_and_drops_stale_frames(self):
        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client, camera=True)
            generation = client._camera_generation
            # Enqueue three frames in one event-loop turn to model a blocked sender.
            for frame in [b"old", b"middle", b"latest"]:
                client._enqueue_camera(frame, time.monotonic(), generation)
            self.assertEqual(await ws.recv(), b"\x01latest")
            client.submit_camera(b"stale", time.monotonic() - 10)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.02)
            client.submit_mic(bytes(640))
            self.assertEqual(await ws.recv(), b"\x00" + bytes(640))

        await self.exercise(scenario, camera_enabled=True)

    async def test_old_camera_callbacks_cannot_survive_disable_or_session_reset(self):
        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client, camera=True)
            old_generation = client._camera_generation
            client._set_camera_enabled(False)
            client._set_camera_enabled(True)
            client._enqueue_camera(b"old", time.monotonic(), old_generation)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(ws.recv(), 0.02)
            client._camera_frame = (b"pending", time.monotonic())
            client._reset_camera()
            self.assertIsNone(client._camera_frame)
            self.assertIsNone(client.camera_status()["last_sent_age_s"])
            client._enqueue_camera(b"old-session", time.monotonic(), old_generation)
            self.assertIsNone(client._camera_frame)

        await self.exercise(scenario, camera_enabled=True)

    async def test_reconnect_reapplies_voice_and_video_and_drops_pending_old_frame(self):
        stop = asyncio.Event()
        result = asyncio.get_running_loop().create_future()
        connections = 0
        client = None

        async def handler(ws):
            nonlocal connections
            connections += 1
            number = connections
            try:
                attach = json.loads(await ws.recv())
                self.assertEqual(attach["action"], "attach_session")
                if number == 2:
                    self.assertEqual(attach["session_id"], "same-session")
                self.assertIsNone(client.voice_id)
                await ws.send(
                    json.dumps(
                        {"action": "session_attached", "data": {"session_id": "same-session"}}
                    )
                )
                self.assertEqual(
                    json.loads(await ws.recv()),
                    {"action": "change_voice", "voice_name": "test-cloned-voice"},
                )
                await ws.send(
                    json.dumps({"action": "voice_changed", "data": {"name": "test-cloned-voice"}})
                )
                self.assertEqual(json.loads(await ws.recv())["action"], "session.update")
                await self.ready(ws, client, camera=True)
                if number == 1:
                    client._camera_frame = (b"old pending frame", time.monotonic())
                    await ws.close()
                else:
                    with self.assertRaises(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), 0.03)
                    client.submit_camera(b"fresh frame", time.monotonic())
                    self.assertEqual(await ws.recv(), b"\x01fresh frame")
                    result.set_result(None)
                    stop.set()
                    await ws.wait_closed()
            except Exception as exc:
                if not result.done():
                    result.set_exception(exc)
                stop.set()

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = MossLive(
                f"http://127.0.0.1:{port}",
                api_key="test-only-key",
                voice_id="test-cloned-voice",
                camera_enabled=True,
                on_audio=lambda pcm: None,
                on_flush=lambda: None,
                on_state=lambda state: None,
                on_text=lambda *args: None,
            )
            task = asyncio.create_task(client.run(stop))
            try:
                await asyncio.wait_for(result, 3)
            finally:
                stop.set()
                await asyncio.wait_for(task, 2)
        self.assertEqual(connections, 2)

    async def test_interrupting_queued_audio_reports_what_was_played(self):
        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client)
            scope = {"run_id": "run-1", "turn_id": "turn-1"}
            await self.speak(
                ws, scope, [("a", bytes(1920)), ("b", bytes(48000)), ("c", bytes(48000))]
            )
            self.assertEqual(json.loads(await ws.recv())["chunk_id"], "a")
            self.assertEqual(len(played), 3)  # Everything is already with the speaker.
            await asyncio.sleep(0.05)
            await ws.send(json.dumps({"action": "response.audio.stop", "data": scope}))
            receipt = json.loads(await ws.recv())
            self.assertEqual(receipt["action"], "playback.stopped")
            # About 50 ms into the 500 ms of "b"; "c" had not started.
            self.assertGreater(receipt["played_audio_ms"], 30)
            self.assertLess(receipt["played_audio_ms"], 450)
            self.assertTrue(flushed)
            await self.assert_quiet(ws, 0.1)
            self.assertFalse(client.is_playing)

        await self.exercise(scenario)

    async def test_requested_output_rate_is_confirmed_before_the_microphone_opens(self):
        async def scenario(ws, client, played, transcript, flushed):
            await ws.send(json.dumps({"action": "session.updated", "data": {"error": ""}}))
            self.assertEqual(
                json.loads(await ws.recv()),
                {
                    "action": "media.configure",
                    "output_audio": {"format": "pcm16", "sample_rate": 16000},
                },
            )
            await self.assert_quiet(ws)
            self.assertFalse(client.connected)
            configured = {"output_audio": {"format": "pcm16", "sample_rate": 16000}, "error": ""}
            await ws.send(json.dumps({"action": "media.configured", "data": configured}))
            await self.opened(ws, client)
            self.assertEqual(client.output_rate, 16000)

            # 640 bytes last 20 ms at 16 kHz; read as 48 kHz they would last under 7 ms.
            scope = {"run_id": "run-1", "turn_id": "turn-1"}
            started = time.monotonic()
            await self.speak(ws, scope, [(str(index), bytes(640)) for index in range(10)])
            for index in range(10):
                self.assertEqual(json.loads(await ws.recv())["chunk_id"], str(index))
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.19)
            self.assertLess(elapsed, 0.6)

        await self.exercise(scenario, output_rate=16000)

    async def refuse_output_rate(self, refusal):
        async def scenario(ws, client, played, transcript, flushed):
            await ws.send(json.dumps({"action": "session.updated", "data": {"error": ""}}))
            self.assertEqual(json.loads(await ws.recv())["action"], "media.configure")
            await ws.send(json.dumps(refusal))
            await self.opened(ws, client)
            self.assertEqual(client.output_rate, 48000)

        with self.assertLogs("mossy.live_client", level="WARNING"):
            client = await self.exercise(scenario, output_rate=16000)
        self.assertIsNone(client.last_error)

    async def test_refused_output_rate_keeps_the_default_and_the_conversation(self):
        await self.refuse_output_rate(
            {"action": "media.configured", "data": {"error": "unsupported sample rate"}}
        )
        await self.refuse_output_rate(
            {"action": "error", "data": {"error": {"code": "invalid_public_request"}}}
        )

    async def test_time_zone_and_voice_tuning_follow_the_handshake(self):
        async def scenario(ws, client, played, transcript, flushed):
            await ws.send(json.dumps({"action": "session.updated", "data": {"error": ""}}))
            await self.opened(
                ws,
                client,
                then=[
                    {
                        "action": "client_context.update",
                        "context": {"time_zone": "Europe/Paris"},
                    },
                    {"action": "media.output.configure", "volume_db": -3.0, "speed": 1.2},
                ],
            )
            # Their replies, even a rejection, leave the conversation running.
            await ws.send(json.dumps({"action": "client_context.updated", "data": {"accepted": 1}}))
            rejected = {"error": {"code": "unsupported_control", "message": "speed is fixed"}}
            await ws.send(json.dumps({"action": "error", "data": rejected}))
            client.submit_mic(bytes(320))
            self.assertEqual(await ws.recv(), b"\x00" + bytes(320))

        with self.assertLogs("mossy.live_client", level="WARNING"):
            client = await self.exercise(
                scenario, time_zone="Europe/Paris", speed=1.2, volume_db=-3
            )
        self.assertIsNone(client.last_error)

    async def test_repeated_confirmations_do_not_reopen_the_microphone(self):
        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client)
            # A resumed session repeats what it already had after the client is ready.
            await ws.send(json.dumps({"action": "voice_changed", "data": {"name": "voice-1"}}))
            await ws.send(json.dumps({"action": "session.updated", "data": {"error": ""}}))
            await self.assert_quiet(ws)
            self.assertEqual(client.voice_id, "voice-1")
            client.submit_mic(bytes(640))
            self.assertEqual(await ws.recv(), b"\x00" + bytes(640))

        await self.exercise(scenario)

    async def test_reply_latency_is_measured_from_the_end_of_speech(self):
        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client)
            scope = {"run_id": "run-1", "turn_id": "turn-1"}
            await ws.send(json.dumps({"action": "input.speech.started"}))
            await ws.send(json.dumps({"action": "input.speech.stopped"}))
            await asyncio.sleep(0.06)
            await ws.send(json.dumps({"action": "input.transcript.done", "data": {"text": "你好"}}))
            await asyncio.sleep(0.06)
            await self.speak(ws, scope, [("a", bytes(1920))])
            self.assertEqual(json.loads(await ws.recv())["chunk_id"], "a")
            stats = client.stats()
            self.assertGreaterEqual(stats["reply_ms"], 110)
            self.assertLess(stats["reply_ms"], 600)
            self.assertEqual(stats["reply_anchor"], "speech_end")
            self.assertEqual(stats["replies"], 1)
            self.assertEqual(stats["late_chunks"], 0)
            self.assertIsNotNone(stats["handshake_ms"])

            # The same reply resumes after its audio ran out: counted, not timed.
            await asyncio.sleep(0.08)
            await self.speak(ws, scope, [("b", bytes(1920))])
            self.assertEqual(json.loads(await ws.recv())["chunk_id"], "b")
            stats = client.stats()
            self.assertEqual((stats["replies"], stats["late_chunks"]), (1, 1))
            self.assertGreater(stats["late_ms_max"], 40)

        await self.exercise(scenario)

    async def test_reply_latency_falls_back_to_the_final_transcript(self):
        async def scenario(ws, client, played, transcript, flushed):
            await self.ready(ws, client)
            await ws.send(json.dumps({"action": "input.transcript.done", "data": {"text": "你好"}}))
            await asyncio.sleep(0.08)
            await self.speak(ws, {"run_id": "run-1", "turn_id": "turn-1"}, [("a", bytes(1920))])
            self.assertEqual(json.loads(await ws.recv())["chunk_id"], "a")
            stats = client.stats()
            self.assertGreaterEqual(stats["reply_ms"], 70)
            self.assertLess(stats["reply_ms"], 600)
            self.assertEqual(stats["reply_anchor"], "transcript")

        await self.exercise(scenario)

    async def converse(self, scripts, *, link_delay=0.0, **options):
        """Run one client through scripted connections, one script per connection.

        ``link_delay`` puts that many seconds between the peers in each
        direction. On loopback a closing handshake finishes at once; on a real
        link it takes a round trip, during which sends already fail.
        """
        stop = asyncio.Event()
        result = asyncio.get_running_loop().create_future()
        connections = []
        client = None

        async def handler(ws):
            connections.append(time.monotonic())
            number = len(connections)
            try:
                attach = json.loads(await ws.recv())
                self.assertEqual(attach["action"], "attach_session")
                await scripts[number - 1](ws, client, attach)
                if number == len(scripts):
                    result.set_result(None)
                    stop.set()
                    await ws.wait_closed()
            except Exception as exc:
                if not result.done():
                    result.set_exception(exc)
                stop.set()

        async def relay(reader, writer):
            loop = asyncio.get_running_loop()
            while data := await reader.read(65536):
                loop.call_later(link_delay, writer.write, data)
            loop.call_later(link_delay, writer.close)

        async def link(reader, writer):
            upstream = await asyncio.open_connection("127.0.0.1", server_port)
            await asyncio.gather(
                relay(reader, upstream[1]), relay(upstream[0], writer), return_exceptions=True
            )

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server_port = server.sockets[0].getsockname()[1]
            if link_delay:
                proxy = await asyncio.start_server(link, "127.0.0.1", 0)
                self.addAsyncCleanup(proxy.wait_closed)
                self.addCleanup(proxy.close)
                port = proxy.sockets[0].getsockname()[1]
            client = MossLive(
                f"http://127.0.0.1:{port}",
                api_key="test-only-key",
                on_audio=lambda pcm: None,
                on_flush=lambda: None,
                on_state=lambda state: None,
                on_text=lambda *args: None,
                **options,
            )
            task = asyncio.create_task(client.run(stop))
            try:
                await asyncio.wait_for(result, 8)
            finally:
                stop.set()
                await asyncio.wait_for(task, 3)
        return client, connections

    async def attached(self, ws, session_id="same-session"):
        await ws.send(
            json.dumps({"action": "session_attached", "data": {"session_id": session_id}})
        )

    async def test_scheduled_server_close_resumes_at_once_without_an_error(self):
        closed_at = []

        async def rotate(ws, client, attach):
            await self.attached(ws)
            self.assertEqual(json.loads(await ws.recv())["action"], "session.update")
            await self.ready(ws, client)

            # Microphone audio keeps flowing, so the close surfaces in a send.
            async def keep_talking():
                while True:
                    client.submit_mic(bytes(640))
                    await asyncio.sleep(0.02)

            async def keep_listening():
                async for _ in ws:
                    pass

            busy = [asyncio.create_task(keep_talking()), asyncio.create_task(keep_listening())]
            await asyncio.sleep(0.02)
            closing = {"code": 1000, "reason": "connection_duration_limit", "reconnect": True}
            await ws.send(json.dumps({"action": "session.closing", "data": closing}))
            closed_at.append(time.monotonic())
            await ws.close(1000, "connection_duration_limit")
            for task in busy:
                task.cancel()

        async def resumed(ws, client, attach):
            self.assertEqual(attach["session_id"], "same-session")
            self.assertIsNone(client.last_error)

        # Three rotations: a growing back-off would show by the second one.
        scripts = [rotate, rotate, rotate, resumed]
        client, connections = await self.converse(scripts, link_delay=0.02)
        gaps = [opened - closed for closed, opened in zip(closed_at, connections[1:], strict=True)]
        # About five round trips each; the old back-off made them 1 s, 2 s and 5 s.
        self.assertLess(max(gaps), 0.9, gaps)
        self.assertEqual(client.stats()["reconnects"], 3)
        self.assertIsNone(client.last_error)

    async def test_changes_while_talking_last_only_if_the_server_confirms_them(self):
        async def first(ws, client, attach):
            await self.attached(ws)
            self.assertEqual(json.loads(await ws.recv())["action"], "session.update")
            await self.ready(ws, client)

            voice = asyncio.create_task(asyncio.to_thread(client.change_voice, "voice-2"))
            self.assertEqual(
                json.loads(await ws.recv()), {"action": "change_voice", "voice_name": "voice-2"}
            )
            self.assertFalse(voice.done())
            await ws.send(json.dumps({"action": "voice_changed", "data": {"name": "voice-2"}}))
            await voice
            self.assertEqual(client.voice_id, "voice-2")

            session = asyncio.create_task(
                asyncio.to_thread(client.update_session, "小安", "你是小安。", "简短回答。")
            )
            self.assertEqual(
                json.loads(await ws.recv()),
                {
                    "action": "session.update",
                    "session": {
                        "persona": {"name": "小安", "prompt": "你是小安。"},
                        "instructions": "简短回答。",
                    },
                },
            )
            rejected = {"action": "session.updated", "data": {"error": "prompt rejected"}}
            await ws.send(json.dumps(rejected))
            with self.assertRaisesRegex(RuntimeError, "prompt rejected"):
                await session

            output = asyncio.create_task(asyncio.to_thread(client.configure_output, 3, 1.2))
            self.assertEqual(
                json.loads(await ws.recv()),
                {"action": "media.output.configure", "volume_db": 3.0, "speed": 1.2},
            )
            unsupported = {"error": {"code": "unsupported_control", "message": "speed is fixed"}}
            await ws.send(json.dumps({"action": "error", "data": unsupported}))
            with self.assertRaisesRegex(RuntimeError, "unsupported_control"):
                await output

            zone = asyncio.create_task(asyncio.to_thread(client.update_time_zone, "Asia/Tokyo"))
            self.assertEqual(
                json.loads(await ws.recv()),
                {"action": "client_context.update", "context": {"time_zone": "Asia/Tokyo"}},
            )
            accepted = {"action": "client_context.updated", "data": {"accepted": True}}
            await ws.send(json.dumps(accepted))
            await zone

            # Rejections leave the conversation running.
            self.assertTrue(client.connected)
            client.submit_mic(bytes(640))
            self.assertEqual(await ws.recv(), b"\x00" + bytes(640))
            await ws.close()

        async def second(ws, client, attach):
            # Only what the server accepted is applied to the next connection.
            await self.attached(ws)
            self.assertEqual(
                json.loads(await ws.recv()), {"action": "change_voice", "voice_name": "voice-2"}
            )
            await ws.send(json.dumps({"action": "voice_changed", "data": {"name": "voice-2"}}))
            update = json.loads(await ws.recv())
            self.assertEqual(update["session"]["persona"], {"name": "MOSS"})
            await ws.send(json.dumps({"action": "session.updated", "data": {"error": ""}}))
            await self.opened(
                ws,
                client,
                then=[{"action": "client_context.update", "context": {"time_zone": "Asia/Tokyo"}}],
            )

        await self.converse([first, second])

    async def test_an_unconfirmed_change_times_out_and_is_not_kept(self):
        async def first(ws, client, attach):
            await self.attached(ws)
            self.assertEqual(json.loads(await ws.recv())["action"], "session.update")
            await self.ready(ws, client)
            with patch("mossy.live_client._REQUEST_TIMEOUT", 0.05):
                with self.assertRaises(TimeoutError):
                    await asyncio.to_thread(client.change_voice, "silent-voice")
            self.assertEqual(json.loads(await ws.recv())["action"], "change_voice")
            await ws.close()

        async def second(ws, client, attach):
            await self.attached(ws)
            self.assertEqual(json.loads(await ws.recv())["action"], "session.update")

        await self.converse([first, second])

    async def test_connection_settings_apply_through_a_new_session(self):
        async def first(ws, client, attach):
            await self.attached(ws)
            self.assertEqual(json.loads(await ws.recv())["action"], "session.update")
            await self.ready(ws, client)
            self.assertEqual(parse_qs(urlsplit(ws.request.path).query)["user"], ["reachy-mini"])
            await asyncio.to_thread(client.reconnect, user="desk-robot", api_key="next-test-key")

        async def second(ws, client, attach):
            self.assertNotIn("session_id", attach)
            self.assertEqual(ws.request.headers["Authorization"], "Bearer next-test-key")
            self.assertEqual(parse_qs(urlsplit(ws.request.path).query)["user"], ["desk-robot"])

        client, connections = await self.converse([first, second])
        self.assertLess(connections[1] - connections[0], 1.0)


class VoiceListTests(unittest.TestCase):
    def test_pages_are_followed_with_the_api_key_in_the_header(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                first = "after=" not in self.path
                page = {
                    "object": "list",
                    "data": [
                        {"id": "voice-1", "object": "audio.voice", "name": "暖暖", "created_at": 1}
                        if first
                        else {"id": "voice-2", "object": "audio.voice", "name": ""}
                    ],
                    "has_more": first,
                    "next_cursor": "cursor-2" if first else None,
                }
                body = json.dumps(page).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/"
            voices = list_voices(url, "test-only-key")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(
            voices,
            [
                {"id": "voice-1", "name": "暖暖", "created_at": 1},
                {"id": "voice-2", "name": "", "created_at": None},
            ],
        )
        self.assertEqual([auth for _, auth in requests], ["Bearer test-only-key"] * 2)
        self.assertEqual(requests[0][0], "/v1/audio/voices?limit=150")
        self.assertIn("after=cursor-2", requests[1][0])
        self.assertNotIn("test-only-key", requests[0][0])


if __name__ == "__main__":
    unittest.main()
