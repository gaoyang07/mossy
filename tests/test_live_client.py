"""Public-protocol integration tests with a local WebSocket peer."""

import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve

from mossy.live_client import MossLive


class LiveProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, scenario, *, on_event=None, camera_enabled=False, voice_id=""):
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

    async def ready(self, ws, client, *, camera=False):
        await ws.send(json.dumps({"action": "session.updated", "data": {"error": ""}}))
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


if __name__ == "__main__":
    unittest.main()
