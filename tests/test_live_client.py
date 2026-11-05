"""Public-protocol integration tests with a local WebSocket peer."""

import asyncio
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve

from mossy.live_client import MossLive


class LiveProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, scenario, *, on_event=None):
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
            self.assertFalse(client.is_playing)
        return client

    async def ready(self, ws, client):
        await ws.send(json.dumps({"action": "session.updated", "data": {"error": ""}}))
        self.assertEqual(json.loads(await ws.recv()), {"action": "mic_state", "enabled": True})
        await asyncio.sleep(0)
        self.assertTrue(client.connected)
        client.submit_mic(bytes(640))
        self.assertEqual(await ws.recv(), bytes(640))

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


if __name__ == "__main__":
    unittest.main()
