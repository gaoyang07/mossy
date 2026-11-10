"""The whole app against a simulated robot and a local MOSS-Live peer.

Needs the Reachy SDK and FastAPI to import, not a robot, daemon or API key.
"""

import asyncio
import importlib.util
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import numpy as np
from websockets.asyncio.server import serve

APP_AVAILABLE = all(
    importlib.util.find_spec(name) for name in ("reachy_mini", "fastapi", "uvicorn", "dotenv")
)
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
WAKE_SECONDS = 0.4
FLUSH_SECONDS = 0.3


def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("condition was not met in time")


class FakeMossLive:
    """Answers configuration requests the way the public protocol describes."""

    def __init__(self):
        self.frames = []  # (time, JSON frame) in arrival order
        self.microphone_chunks = 0
        self._ws = None
        self._loop = asyncio.new_event_loop()
        started = threading.Event()

        async def main():
            self._stop = asyncio.Event()
            async with serve(self._handle, "127.0.0.1", 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                started.set()
                await self._stop.wait()

        threading.Thread(target=self._loop.run_until_complete, args=(main(),), daemon=True).start()
        started.wait(5)

    async def _handle(self, ws):
        self._ws = ws
        async for message in ws:
            if isinstance(message, bytes):
                self.microphone_chunks += message[:1] == b"\x00"
                continue
            frame = json.loads(message)
            self.frames.append((time.monotonic(), frame))
            action = frame["action"]
            if action == "attach_session":
                reply = ("session_attached", {"session_id": "app-test"})
            elif action == "change_voice":
                reply = ("voice_changed", {"name": frame["voice_name"]})
            elif action == "session.update":
                refused = "拒绝" in frame["session"]["persona"].get("prompt", "")
                reply = ("session.updated", {"error": "prompt rejected" if refused else ""})
            elif action == "media.configure":
                reply = ("media.configured", {"output_audio": frame["output_audio"], "error": ""})
            elif action == "media.output.configure":
                reply = ("media.output.configured", {"speed": frame["speed"]})
            elif action == "client_context.update":
                reply = ("client_context.updated", {"accepted": True})
            else:
                continue
            await ws.send(json.dumps({"action": reply[0], "data": reply[1]}))

    def send(self, *messages):
        async def send():
            for message in messages:
                await self._ws.send(message if isinstance(message, bytes) else json.dumps(message))

        asyncio.run_coroutine_threadsafe(send(), self._loop).result(5)

    def speak(self, scope, chunks):
        frames = []
        for chunk_id, pcm in chunks:
            data = {"chunk_id": chunk_id, **scope}
            frames += [{"action": "response.audio.context", "data": data}, pcm]
        self.send(*frames)

    def received(self, action, since=0):
        return [(at, frame) for at, frame in self.frames[since:] if frame["action"] == action]

    def close(self):
        self._loop.call_soon_threadsafe(self._stop.set)


class FakeRobot:
    """The parts of ReachyMini and its media manager that the app touches."""

    def __init__(self, during_wake_up):
        self.calls = []  # (name, time) in call order
        self.pushed = []  # (time, samples) handed to the speaker
        self.poses = []
        self.media = self
        self.audio = self
        self.client = type("Client", (), {"host": "127.0.0.1", "port": 9})()
        self._during_wake_up = during_wake_up

    def _record(self, name):
        self.calls.append((name, time.monotonic()))

    def names(self):
        return [name for name, _ in self.calls]

    def wake_up(self):
        self._record("wake_up")
        self._during_wake_up()
        time.sleep(WAKE_SECONDS)
        self._record("awake")

    def get_audio_sample(self):
        time.sleep(0.02)
        return np.full((320, 2), 0.25, dtype=np.float32)

    def get_frame(self):
        return np.zeros((48, 64, 3), dtype=np.uint8)

    def push_audio_sample(self, samples):
        self.pushed.append((time.monotonic(), samples))

    def clear_player(self):
        self._record("clear_player")
        time.sleep(FLUSH_SECONDS)  # A flush can be a blocking call to the daemon.
        self._record("cleared")

    def set_target(self, *, head, antennas):
        self.poses.append((head, antennas))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self._record(name)


class FakeMove:
    duration = 1.0

    def evaluate(self, t):
        return np.eye(4), np.array([-0.3, 0.3]), 0.0


if APP_AVAILABLE:
    from dotenv import dotenv_values


@unittest.skipUnless(APP_AVAILABLE, "needs the Reachy SDK, FastAPI and uvicorn")
class AppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from mossy import config, main

        cls.config = config
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.env_file = Path(directory.name) / ".env"
        cls.server = FakeMossLive()
        cls.addClassCleanup(cls.server.close)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"
        settings = {
            **config.load({}),
            "MOSS_API_KEY": "test-only-key",
            "MOSS_LIVE_URL": f"http://127.0.0.1:{cls.server.port}",
            "MOSS_LIVE_VOICE_ID": "voice-1",
            "MOSS_HALF_DUPLEX": False,
            "MOSS_WEB_UI_PORT": port,
            "MOSS_TIME_ZONE": "",
        }
        moves = {kind: FakeMove() for kind in ("happy", "agree", "thinking", "listening")}
        for patcher in (
            patch.dict(main.SETTINGS, settings),
            patch.object(config, "USER_ENV", cls.env_file),
            patch.object(config, "_EXPORTED", frozenset({"MOSS_LIVE_MODEL"})),
            patch("mossy.expressions.load_moves", return_value=moves),
        ):
            patcher.start()
            cls.addClassCleanup(patcher.stop)

        cls.seen_while_waking = {}

        def during_wake_up():
            # What is already up while the robot is still lifting its head.
            cls.seen_while_waking["page"] = cls.get("/state")[0]
            wait_until(lambda: cls.server.received("mic_state"))
            cls.seen_while_waking["cloud"] = time.monotonic()

        cls.robot = FakeRobot(during_wake_up)
        cls.app = main.Moss()
        assert cls.app._live._base_url.startswith("http://127.0.0.1:"), "never the real service"
        cls.thread = threading.Thread(target=cls.app.run, args=(cls.robot, cls.app.stop_event))
        cls.thread.start()
        cls.addClassCleanup(cls.stop_app)
        wait_until(lambda: cls.get("/state")[1].get("stats", {}).get("ready_s"), 10)

    @classmethod
    def stop_app(cls):
        cls.app.stop_event.set()
        cls.thread.join(10)

    @classmethod
    def request(cls, route, body=None, headers=None, method=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            cls.base + route,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        for _ in range(100):
            try:
                with HTTP.open(request, timeout=10) as response:
                    return response.status, json.loads(response.read() or b"null")
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read() or b"null")
            except urllib.error.URLError:
                time.sleep(0.02)  # The page is still starting.
        raise AssertionError("the page never answered")

    @classmethod
    def get(cls, route):
        return cls.request(route)

    def test_cloud_and_page_come_up_while_the_robot_wakes(self):
        calls = dict(self.robot.calls)
        self.assertEqual(self.seen_while_waking["page"], 200)
        self.assertLess(self.seen_while_waking["cloud"], calls["awake"])
        # Audio starts only once the wake-up sound has had the device to itself.
        names = self.robot.names()
        self.assertLess(names.index("awake"), names.index("start_playing"))
        self.assertLess(names.index("start_playing"), names.index("start_recording"))
        self.assertEqual(names[0], "enable_motors")
        wait_until(lambda: self.server.microphone_chunks)

    def test_handshake_asks_for_the_robot_sample_rate(self):
        configure = self.server.received("media.configure")[0][1]
        self.assertEqual(configure["output_audio"], {"format": "pcm16", "sample_rate": 16000})
        status, state = self.get("/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["connected"])
        self.assertEqual(state["voice_id"], "voice-1")
        self.assertEqual(state["stats"]["output_rate"], 16000)
        self.assertTrue(state["full_duplex"])

    def test_speech_is_played_in_order_and_an_interruption_never_waits_for_the_flush(self):
        scope = {"run_id": "run-1", "turn_id": "turn-1"}
        first = (np.arange(320, dtype="<i2") * 50).tobytes()  # 20 ms at 16 kHz
        second = (np.arange(320, dtype="<i2") * -50).tobytes()
        before = len(self.robot.pushed)
        frames = len(self.server.frames)
        self.server.speak(scope, [("a", first), ("b", second), ("c", bytes(32000))])
        wait_until(lambda: len(self.robot.pushed) >= before + 3)
        played = [samples for _, samples in self.robot.pushed[before:]]
        self.assertEqual([chunk.dtype for chunk in played], [np.float32] * 3)
        np.testing.assert_allclose(played[0], np.arange(320) * 50 / 32768, atol=1e-6)
        np.testing.assert_allclose(played[1], np.arange(320) * -50 / 32768, atol=1e-6)
        self.assertEqual(len(played[2]), 16000)

        wait_until(lambda: len(self.server.received("playback.chunk.completed", frames)) == 2)
        stopped_at = time.monotonic()
        self.server.send({"action": "response.audio.stop", "data": scope})
        at, receipt = wait_until(lambda: self.server.received("playback.stopped", frames))[0]
        # The flush takes FLUSH_SECONDS; the receipt must not wait for it.
        self.assertLess(at - stopped_at, FLUSH_SECONDS / 2)
        self.assertEqual(receipt["run_id"], "run-1")

        # A new reply is queued behind the flush instead of being wiped by it.
        next_scope = {"run_id": "run-2", "turn_id": "turn-2"}
        self.server.speak(next_scope, [("d", first)])
        wait_until(lambda: len(self.robot.pushed) == before + 4)
        cleared = [at for name, at in self.robot.calls if name == "cleared"][-1]
        self.assertGreater(self.robot.pushed[-1][0], cleared)
        self.server.send({"action": "response.audio.done", "data": next_scope})
        wait_until(lambda: self.server.received("playback.completed", frames))

    def test_state_skips_a_transcript_the_page_already_has(self):
        text = {"action": "input.transcript.done", "data": {"text": "你好，MOSS"}}
        self.server.send(text)
        line = {"role": "user", "text": "你好，MOSS", "final": True}

        def state_with_line():
            state = self.get("/state")[1]
            return state if line in state["transcript"] else None

        state = wait_until(state_with_line)
        again = self.get(f"/state?version={state['transcript_version']}")[1]
        self.assertIsNone(again["transcript"])
        self.assertEqual(again["transcript_version"], state["transcript_version"])

    def test_settings_are_described_without_the_api_key(self):
        status, body = self.get("/api/settings")
        self.assertEqual(status, 200)
        self.assertNotIn("test-only-key", json.dumps(body))
        fields = {field["key"]: field for field in body["fields"]}
        self.assertTrue(fields["MOSS_API_KEY"]["is_set"])
        self.assertEqual(fields["MOSS_LIVE_VOICE_ID"]["value"], "voice-1")
        self.assertTrue(fields["MOSS_LIVE_MODEL"]["pinned"])
        self.assertEqual(set(body["groups"]), {field["group"] for field in body["fields"]})

    def test_a_confirmed_change_applies_now_and_is_saved(self):
        frames = len(self.server.frames)
        status, body = self.request(
            "/api/settings", {"MOSS_LIVE_SPEED": 1.2, "MOSS_POSTURE": "relaxed", "MOSS_API_KEY": ""}
        )
        self.assertEqual((status, body["restart"]), (200, False))
        self.assertEqual(set(body["saved"]), {"MOSS_LIVE_SPEED", "MOSS_POSTURE"})
        configure = self.server.received("media.output.configure", frames)[0][1]
        self.assertEqual(configure["speed"], 1.2)
        wait_until(lambda: self.get("/state")[1]["expressions"]["posture"] == "relaxed")
        saved = self.config.load(dotenv_values(self.env_file))
        self.assertEqual((saved["MOSS_LIVE_SPEED"], saved["MOSS_POSTURE"]), (1.2, "relaxed"))
        self.assertEqual(saved["MOSS_API_KEY"], "")  # An empty secret field changes nothing.
        self.assertEqual(self.app.settings["MOSS_API_KEY"], "test-only-key")

    def test_a_rejected_change_is_reported_and_not_saved(self):
        status, body = self.request(
            "/api/settings", {"MOSS_LIVE_PROMPT": "请拒绝这个人设", "MOSS_AUDIO_GAIN": 1.5}
        )
        self.assertEqual(status, 502)
        self.assertIn("prompt rejected", body["error"])
        self.assertEqual(self.app.settings["MOSS_LIVE_PROMPT"], "")
        self.assertNotIn("MOSS_LIVE_PROMPT", dotenv_values(self.env_file))
        self.assertTrue(self.get("/state")[1]["connected"])

    def test_invalid_pinned_and_unknown_settings_are_refused(self):
        for values in (
            {"MOSS_LIVE_SPEED": 3},
            {"MOSS_POSTURE": "handstand"},
            {"MOSS_LIVE_MODEL": "another-model"},
            {"MOSS_NOT_A_SETTING": 1},
        ):
            with self.subTest(values=values):
                status, body = self.request("/api/settings", values)
                self.assertEqual(status, 400)
                self.assertIn(next(iter(values)), body["error"])
        restart = self.request("/api/settings", {"MOSS_WEB_UI_PORT": 7999})[1]["restart"]
        self.assertTrue(restart)

    def test_settings_and_controls_are_refused_from_other_sites(self):
        # A page on another site that the user has open, and a name that was
        # re-pointed at this machine: both reach the port from a local browser.
        elsewhere = {"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"}
        rebound = {"Host": "attacker.example"}
        muted = self.app.muted
        gestures = self.get("/state")[1]["expressions"]["enabled"]
        for route, body in (
            ("/api/settings", None),
            ("/api/settings", {"MOSS_LIVE_SPEED": 0.8}),
            ("/api/voices", None),
            ("/api/gesture/agree", {}),
            ("/api/app/stop", {}),
            ("/mute", {}),
            ("/expressions/toggle", {}),
            ("/camera/toggle", {}),
        ):
            for headers in (elsewhere, rebound):
                with self.subTest(route=route, write=body is not None, headers=headers):
                    self.assertEqual(self.request(route, body, headers=headers)[0], 403)
        self.assertFalse(self.app.stop_event.is_set())
        self.assertEqual(self.app.muted, muted)
        self.assertEqual(self.get("/state")[1]["expressions"]["enabled"], gestures)

    def test_the_page_is_told_whether_its_browser_may_use_the_controls(self):
        self.assertIs(self.get("/state")[1]["local_access"], True)
        rebound = self.request("/state", headers={"Host": "attacker.example"})
        self.assertEqual(rebound[0], 200)  # Status stays readable; the controls do not.
        self.assertIs(rebound[1]["local_access"], False)

    def test_microphone_and_gesture_switches_work_from_this_machine(self):
        muted = self.app.muted
        self.assertEqual(self.request("/mute", {}), (200, {"muted": not muted}))
        self.assertEqual(self.get("/state")[1]["muted"], not muted)
        self.assertEqual(self.request("/mute", {}), (200, {"muted": muted}))

        enabled = self.get("/state")[1]["expressions"]["enabled"]
        self.assertEqual(self.request("/expressions/toggle", {}), (200, {"enabled": not enabled}))
        wait_until(lambda: self.get("/state")[1]["expressions"]["enabled"] is not enabled)
        self.assertEqual(self.request("/expressions/toggle", {}), (200, {"enabled": enabled}))
        wait_until(lambda: self.get("/state")[1]["expressions"]["enabled"] is enabled)

    def test_a_gesture_can_be_previewed(self):
        wait_until(lambda: self.get("/state")[1]["expressions"]["available"])
        started = self.get("/state")[1]["expressions"]["started"]
        self.assertEqual(self.request("/api/gesture/agree", {})[0], 200)
        wait_until(lambda: self.get("/state")[1]["expressions"]["started"] == started + 1)
        self.assertEqual(self.request("/api/gesture/moonwalk", {})[0], 400)


@unittest.skipUnless(APP_AVAILABLE, "needs the Reachy SDK, FastAPI and uvicorn")
class ShutdownTests(unittest.TestCase):
    def run_app(self, **overrides):
        from mossy import config, main

        server = FakeMossLive()
        self.addCleanup(server.close)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        settings = {
            **config.load({}),
            "MOSS_API_KEY": "test-only-key",
            "MOSS_LIVE_URL": f"http://127.0.0.1:{server.port}",
            "MOSS_WEB_UI_PORT": port,
            "MOSS_EXPRESSIONS": False,
            **overrides,
        }
        robot = FakeRobot(lambda: None)
        with patch.dict(main.SETTINGS, settings):
            with patch("mossy.expressions.load_moves", return_value={}):
                app = main.Moss()
                thread = threading.Thread(target=app.run, args=(robot, app.stop_event))
                thread.start()
                self.addCleanup(thread.join, 10)
                self.addCleanup(app.stop_event.set)
                return app, robot, server, thread, f"http://127.0.0.1:{port}"

    def test_stopping_from_the_page_puts_the_robot_to_sleep(self):
        app, robot, server, thread, base = self.run_app(MOSS_WAKE_ANIMATION=False)
        wait_until(lambda: "start_recording" in robot.names(), 10)
        self.assertNotIn("wake_up", robot.names())  # The short wake-up was requested.
        self.assertIn("goto_target", robot.names())
        request = urllib.request.Request(base + "/api/app/stop", data=b"{}", method="POST")
        for _ in range(100):
            try:
                HTTP.open(request, timeout=5).close()
                break
            except urllib.error.URLError:
                time.sleep(0.02)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(robot.names()[-3:], ["stop_playing", "goto_sleep", "disable_motors"])

    def test_a_silent_room_ends_the_session_after_the_idle_limit(self):
        with self.assertLogs("mossy.main", level="INFO") as logs:
            app, robot, server, thread, base = self.run_app(MOSS_IDLE_SHUTDOWN_MIN=0.01)  # 0.6 s
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIn("start_recording", robot.names())
        self.assertTrue(any("going to sleep" in line for line in logs.output))
        self.assertEqual(robot.names()[-2:], ["goto_sleep", "disable_motors"])


if __name__ == "__main__":
    unittest.main()
