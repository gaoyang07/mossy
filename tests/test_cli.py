"""Exercise the mossy command's service decisions without hardware or a daemon."""

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from mossy import cli

ROOT = Path(__file__).resolve().parents[1]


class Services:
    def __init__(self, daemon_state="running", app=None):
        self.daemon = {"state": daemon_state, "error": None} if daemon_state else None
        self.app = app
        self.ready = {"connected": True, "full_duplex": True}
        self.calls = []
        self.recovery_state = "running"

    def request(self, route, *, base=None, method="GET", optional=False):
        self.calls.append((method, route))
        if method == "GET":
            if route == "/api/daemon/status":
                return self.daemon
            if route == "/api/apps/current-app-status":
                return self.app
            if route == "/state":
                return self.ready
            if route == "/api/apps/job-status/test-job":
                return {"status": "done"}
        if method == "POST":
            if route == "/api/apps/stop-current-app":
                self.app = None
                return None
            if route.startswith("/api/daemon/"):
                self.daemon = {"state": self.recovery_state, "error": "test motor error"}
                return {"job_id": "test-job"}
            if route in ("/api/apps/start-app/mossy", "/api/apps/restart-current-app"):
                self.app = {"info": {"name": "mossy"}, "state": "running"}
                return self.app
        raise AssertionError((method, route))

    @property
    def posts(self):
        return [route for method, route in self.calls if method == "POST"]


DAEMON_COMMAND = "/runtime/.venv/bin/python3 -m reachy_mini.daemon.app.main --desktop-app-daemon"


class Robot(Services):
    """The daemon, the app's page and the robot itself, as far as stopping goes.

    Follows reachy_mini 1.8.4. Asked to stop an app, the daemon waits for it to
    exit, then turns the motors on and moves the robot to its zero pose. That
    leaves its reported motor mode stale, and setting the mode it already
    reports does nothing. Told to terminate, the daemon puts the robot to sleep
    and releases the motors before its process goes away.

    ``owned`` says whether ``mossy start`` launched the daemon: then it is
    process 4242 and the PID record names it. Otherwise it is process 9000,
    started by something else, and there is no record.
    """

    def __init__(self, app=None, *, page=None, obeys=True, owned=False):
        super().__init__(app=app)
        self.page = app is not None if page is None else page  # Does the app's page answer?
        self.obeys = obeys  # Does the app exit when its page asks it to?
        self.asleep, self.torque, self.mode = False, True, "enabled"
        self.sleep_fails = False
        self.now = [0.0]  # The clock the command sees; its sleeps advance it.
        self.processes = {4242 if owned else 9000: DAEMON_COMMAND}  # What ps shows.
        self.record = "4242\n" if owned else None  # What the PID record holds.
        self.state = None  # The state directory, once the command has run.
        self.signals = []  # (pid, signal) that reached a process.
        self.shutdown_seconds = 6.0  # From SIGTERM to gone; None never exits.
        self.vanishes = False  # Does the daemon exit just before it is signaled?
        self.exits_at = None
        self.stopped_at = None  # When the daemon ends a stop begun elsewhere; None never.

    def daemon_exits(self):
        self.processes = {p: c for p, c in self.processes.items() if c != DAEMON_COMMAND}
        if self.daemon and self.daemon["state"] == "running":
            self.asleep, self.torque, self.mode = True, False, "disabled"
        self.daemon = self.app = self.exits_at = None  # Its HTTP API is gone too.

    def ps(self, argv, **kwargs):
        if self.exits_at is not None and self.now[0] >= self.exits_at:
            self.daemon_exits()
        command = self.processes.get(int(argv[argv.index("-p") + 1]), "")
        return Mock(stdout=command and command + "\n", returncode=0 if command else 1)

    def kill(self, pid, sig):
        if self.vanishes:
            self.daemon_exits()
        if pid not in self.processes:
            raise ProcessLookupError(3, "No such process")
        assert self.processes[pid] == DAEMON_COMMAND, "an unrelated process was signaled"
        assert pid == 4242, "a daemon that mossy did not start was signaled"
        still_running = self.page or (self.app and self.app["state"] not in ("done", "error"))
        assert not still_running, "the daemon was signaled while an app was still running"
        self.signals.append((pid, sig))
        if self.shutdown_seconds is not None and self.exits_at is None:
            self.exits_at = self.now[0] + self.shutdown_seconds

    @property
    def recorded(self):
        return (self.state / "reachy-daemon.pid").exists()

    def app_exits(self):
        # mossy puts the robot to sleep and releases the motors on its way out.
        self.page, self.asleep, self.torque, self.mode = False, True, False, "disabled"

    def request(self, route, *, base=None, method="GET", optional=False):
        stopping = self.app and self.app["state"] == "stopping"
        if stopping and self.stopped_at is not None and self.now[0] >= self.stopped_at:
            self.app, self.page = None, False
            self.asleep, self.torque = False, True  # The daemon's zero pose, motors on.
        if route in ("/state", "/api/app/stop") and not self.page:
            self.calls.append((method, route))
            return None  # Nothing listens on the page's port.
        if (method, route) == ("POST", "/api/app/stop"):
            self.calls.append((method, route))
            if self.obeys:
                self.app_exits()
                self.app = self.app and {**self.app, "state": "done"}
            return {"action": "stop"}
        if (method, route) == ("POST", "/api/apps/stop-current-app"):
            self.calls.append((method, route))
            if self.app and self.app["state"] != "error":
                self.app_exits()
            self.app, self.page = None, False
            self.asleep, self.torque = False, True  # Back to the zero pose, mode unchanged.
            return None
        if method == "POST" and route.startswith("/api/motors/set_mode/"):
            self.calls.append((method, route))
            mode = route.rsplit("/", 1)[1]
            if mode != self.mode:
                self.mode, self.torque = mode, mode == "enabled"
            return {"status": mode}
        if (method, route) == ("POST", "/api/move/play/goto_sleep"):
            self.calls.append((method, route))
            if self.sleep_fails:
                raise cli.StartupError("接口 /api/move/play/goto_sleep 返回 HTTP 503。")
            assert self.torque, "the robot cannot move with its motors off"
            self.asleep = True
            return {"uuid": "sleep-move"}
        if (method, route) == ("GET", "/api/move/running"):
            self.calls.append((method, route))
            return []
        return super().request(route, base=base, method=method, optional=optional)

    @property
    def resting(self):
        return self.asleep and not self.torque


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.launcher = cli
        self.base = "http://127.0.0.1:7863"
        self.running = {"info": {"name": "mossy"}, "state": "running"}

    def run_start(self, services, **kwargs):
        with patch.object(self.launcher, "request", services.request):
            with contextlib.redirect_stdout(io.StringIO()):
                return self.launcher.start(self.base, **kwargs)

    def test_healthy_app_is_reused_without_mutation(self):
        services = Services(app=self.running)
        ready = self.run_start(services)
        self.assertTrue(ready["connected"])
        self.assertEqual(services.posts, [])

    def test_cold_start_spawns_once_then_starts_mossy(self):
        services = Services(daemon_state=None)

        def spawn():
            services.daemon = {"state": "running", "error": None}
            return Mock()

        with patch.object(
            self.launcher.socket, "create_connection", side_effect=ConnectionRefusedError
        ):
            with patch.object(self.launcher, "spawn_daemon", side_effect=spawn) as spawner:
                self.assertTrue(self.run_start(services)["connected"])
        spawner.assert_called_once()
        self.assertEqual(services.posts, ["/api/apps/start-app/mossy"])

    def test_daemon_error_stops_own_app_before_recovery(self):
        services = Services(daemon_state="error", app=self.running)
        self.assertTrue(self.run_start(services)["connected"])
        self.assertEqual(
            services.posts,
            ["/api/apps/stop-current-app", "/api/daemon/restart", "/api/apps/start-app/mossy"],
        )

    def test_other_app_is_not_stopped_even_if_daemon_has_error(self):
        services = Services(daemon_state="error", app={"info": {"name": "another_app"}})
        with self.assertRaises(self.launcher.StartupError):
            self.run_start(services)
        self.assertEqual(services.posts, [])

    def test_done_recovery_job_is_not_enough_if_motors_remain_in_error(self):
        services = Services(daemon_state="stopped")
        services.recovery_state = "error"
        with self.assertRaisesRegex(self.launcher.StartupError, "机器人尚未就绪"):
            self.run_start(services)
        self.assertEqual(services.posts, ["/api/daemon/start?wake_up=false"])

    def test_existing_uninitialized_daemon_is_started(self):
        services = Services(daemon_state="not_initialized")
        self.assertTrue(self.run_start(services)["connected"])
        self.assertEqual(
            services.posts,
            ["/api/daemon/start?wake_up=false", "/api/apps/start-app/mossy"],
        )

    def test_occupied_port_does_not_spawn_another_daemon(self):
        services = Services(daemon_state=None)
        with patch.object(self.launcher.socket, "create_connection"):
            with patch.object(self.launcher, "spawn_daemon") as spawner:
                with self.assertRaisesRegex(self.launcher.StartupError, "8000"):
                    self.run_start(services)
        spawner.assert_not_called()

    def test_app_that_ended_on_its_own_is_started_rather_than_restarted(self):
        # After an idle shutdown or a stop from the page the daemon still lists it.
        services = Services(app={"info": {"name": "mossy"}, "state": "done"})
        self.assertTrue(self.run_start(services)["connected"])
        self.assertEqual(services.posts, ["/api/apps/start-app/mossy"])

    def test_cloud_errors_are_shown_once_while_waiting(self):
        services = Services(app=self.running)
        answers = iter([{"connected": False, "error": "HTTP 401"}] * 3 + [services.ready])
        original = services.request

        def request(route, **kwargs):
            return next(answers) if route == "/state" else original(route, **kwargs)

        output = io.StringIO()
        with patch.object(self.launcher, "request", request):
            with patch.object(self.launcher.time, "sleep"):
                with contextlib.redirect_stdout(output):
                    self.assertTrue(self.launcher.wait_connected(self.base, 30)["connected"])
        self.assertEqual(output.getvalue().count("HTTP 401"), 1)

    def test_explicit_restart_only_restarts_mossy(self):
        services = Services(app=self.running)
        self.assertTrue(self.run_start(services, restart=True)["connected"])
        self.assertEqual(services.posts, ["/api/apps/restart-current-app"])

    def test_disconnected_app_gets_one_restart_after_grace_period(self):
        services = Services(app=self.running)
        with patch.object(self.launcher, "wait_connected", side_effect=[None, services.ready]):
            self.assertTrue(self.run_start(services)["connected"])
        self.assertEqual(services.posts, ["/api/apps/restart-current-app"])

    def test_failed_new_start_stops_its_app(self):
        services = Services()
        with patch.object(self.launcher, "wait_connected", return_value=None):
            with self.assertRaises(self.launcher.StartupError):
                self.run_start(services)
        self.assertEqual(
            services.posts, ["/api/apps/start-app/mossy", "/api/apps/stop-current-app"]
        )

    def test_failure_cleanup_preserves_an_app_that_took_over(self):
        services = Services()

        def replaced_app(*args):
            services.app = {"info": {"name": "another_app"}}
            raise self.launcher.StartupError("startup failed")

        with patch.object(self.launcher, "wait_connected", side_effect=replaced_app):
            with self.assertRaises(self.launcher.StartupError):
                self.run_start(services)
        self.assertEqual(services.posts, ["/api/apps/start-app/mossy"])

    def test_proxy_bypass_preserves_existing_entries(self):
        with patch.dict(self.launcher.os.environ, {"NO_PROXY": "example.test"}):
            env = self.launcher.direct_environment()
        self.assertIn("example.test", env["NO_PROXY"].split(","))
        for name in ("NO_PROXY", "no_proxy"):
            self.assertTrue({"localhost", "127.0.0.1", "::1"} <= set(env[name].split(",")))

    def test_child_python_does_not_inherit_bootstrapped_gstreamer_paths(self):
        inherited = {
            "GST_PLUGIN_SCANNER_1_0": "/parent/gst-plugin-scanner",
            "GST_REGISTRY_1_0": "/parent/registry.bin",
            "GST_PYTHONPATH_1_0": "/parent/gi",
            "GST_PLUGIN_PATH_1_0": "/parent/plugins",
            "GST_PLUGIN_SYSTEM_PATH_1_0": "/parent/plugins",
            "GI_TYPELIB_PATH": "/parent/typelib",
            "PYGI_DLL_DIRS": "/parent/bin",
            "XDG_DATA_DIRS": "/parent/share",
            "XDG_CONFIG_DIRS": "/parent/etc",
            "PYTHONPATH": "/parent/site-packages",
            "PYTHONHOME": "/parent/python",
        }
        with patch.dict(self.launcher.os.environ, {**inherited, "GST_DEBUG": "2"}):
            env = self.launcher.direct_environment()
        self.assertFalse(set(inherited) & set(env))
        self.assertEqual(env["GST_DEBUG"], "2")

    def test_spawn_uses_the_isolated_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(self.launcher, "STATE", Path(temp)):
                with patch.dict(self.launcher.os.environ, {"GST_PLUGIN_SCANNER_1_0": "/bad/path"}):
                    with patch.object(
                        self.launcher.subprocess, "Popen", return_value=Mock(pid=123)
                    ) as popen:
                        with contextlib.redirect_stdout(io.StringIO()):
                            self.launcher.spawn_daemon()
        self.assertNotIn("GST_PLUGIN_SCANNER_1_0", popen.call_args.kwargs["env"])

    def test_cold_daemon_initialization_can_exceed_45_seconds(self):
        now = [0.0]

        def request(*args, **kwargs):
            return {"state": "running"} if now[0] >= 60 else None

        with patch.object(self.launcher, "request", side_effect=request):
            with patch.object(self.launcher.time, "monotonic", side_effect=lambda: now[0]):
                with patch.object(
                    self.launcher.time,
                    "sleep",
                    side_effect=lambda dt: now.__setitem__(0, now[0] + dt),
                ):
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = self.launcher.wait_daemon(Mock(poll=Mock(return_value=None)))
        self.assertEqual(result["state"], "running")
        self.assertGreaterEqual(now[0], 60)

    def test_cold_app_initialization_can_exceed_45_seconds(self):
        now = [0.0]
        services = Services()
        original_request = services.request

        def request(route, **kwargs):
            if route == "/state" and now[0] < 60:
                return None
            return original_request(route, **kwargs)

        services.request = request
        with patch.object(self.launcher.time, "monotonic", side_effect=lambda: now[0]):
            with patch.object(
                self.launcher.time, "sleep", side_effect=lambda dt: now.__setitem__(0, now[0] + dt)
            ):
                self.assertTrue(self.run_start(services)["connected"])
        self.assertGreaterEqual(now[0], 60)
        self.assertEqual(services.posts, ["/api/apps/start-app/mossy"])

    def test_background_process_detaches_and_logs_without_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(self.launcher, "STATE", Path(temp)):
                with patch.object(
                    self.launcher.subprocess, "Popen", return_value=Mock(pid=123)
                ) as popen:
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.launcher.spawn_daemon()
                    self.assertTrue(popen.call_args.kwargs["start_new_session"])
                    self.assertEqual(
                        popen.call_args.kwargs["stdin"], self.launcher.subprocess.DEVNULL
                    )
                    self.assertEqual((Path(temp) / "reachy-daemon.pid").read_text(), "123\n")
                    log = next(Path(temp).glob("*.log"))
                    self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_duplicate_click_returns_without_starting_services(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(self.launcher, "STATE", Path(temp)):
                with patch.object(self.launcher.sys, "argv", ["mossy", "start", "--no-browser"]):
                    with patch.object(self.launcher.fcntl, "flock", side_effect=BlockingIOError):
                        with patch.object(self.launcher, "start") as start:
                            with contextlib.redirect_stdout(io.StringIO()):
                                self.assertEqual(self.launcher.main(), 0)
                            start.assert_not_called()

    def test_error_messages_redact_api_keys(self):
        secret = "sk-" + "a" * 48
        self.assertNotIn(secret, self.launcher.safe_message("error: " + secret))


class CommandTests(unittest.TestCase):
    def setUp(self):
        # A test must never read the real PID record or signal a real process.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        real = AssertionError("a real process would have been signaled")
        for patcher in (
            patch.object(cli, "STATE", Path(directory.name)),
            patch.object(cli.os, "kill", side_effect=real),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, *argv):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = cli.main(list(argv))
            except SystemExit as exit:
                code = exit.code
        return code, output.getvalue(), errors.getvalue()


class ServiceCommandTests(CommandTests):
    def test_no_arguments_and_help_list_every_command(self):
        for argv in ((), ("help",), ("--help",)):
            with self.subTest(argv=argv):
                code, output, _ = self.run_cli(*argv)
                self.assertEqual(code, 0)
                for command in ("start", "stop", "end", "restart", "status", "gui", "config"):
                    self.assertIn(command, output)
        for command in ("stop", "end"):
            code, output, _ = self.run_cli("help", command)
            self.assertEqual(code, 0)
            self.assertIn("--keep-daemon", output)
            self.assertIn("--daemon", output)

    def test_start_restart_and_gui_share_the_start_flow(self):
        parser = cli.build_parser()
        start = parser.parse_args(["start", "--no-browser"])
        restart = parser.parse_args(["restart"])
        self.assertEqual((start.restart, start.no_browser), (False, True))
        self.assertEqual((restart.restart, restart.no_browser), (True, False))
        self.assertIs(start.run, restart.run)
        seen = []
        with patch.object(cli, "cmd_start", side_effect=lambda args: seen.append(args) or 0):
            self.assertEqual(cli.cmd_gui(parser.parse_args(["gui"])), 0)
        opened = (seen[0].restart, seen[0].no_browser, seen[0].page)
        self.assertEqual(opened, (False, False, "#settings"))

    def stop(self, robot, *argv):
        """Run ``mossy stop`` against a simulated daemon, page, robot and process table."""
        if robot.state is None:
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            robot.state = Path(directory.name)
            if robot.record is not None:
                (robot.state / "reachy-daemon.pid").write_text(robot.record)
        now = robot.now
        patches = (
            patch.object(cli, "request", robot.request),
            patch.object(cli, "installed_app", return_value={"port": 7863}),
            patch.object(cli, "STATE", robot.state),
            patch.object(cli.subprocess, "run", side_effect=robot.ps),
            patch.object(cli.os, "kill", side_effect=robot.kill),
            patch.object(cli.time, "monotonic", side_effect=lambda: now[0]),
            patch.object(cli.time, "sleep", side_effect=lambda dt: now.__setitem__(0, now[0] + dt)),
        )
        with contextlib.ExitStack() as stack:
            for each in patches:
                stack.enter_context(each)
            return self.run_cli(*(argv or ("stop",)))

    def test_stop_and_end_leave_the_robot_asleep_with_its_motors_released(self):
        for command in ("stop", "end"):
            with self.subTest(command=command):
                robot = Robot(app={"info": {"name": "mossy"}, "state": "running"})
                code, output, _ = self.stop(robot, command)
                self.assertEqual(code, 0)
                self.assertTrue(robot.resting)
                # Asked to stop the app, the daemon would wake the robot again.
                self.assertEqual(robot.posts, ["/api/app/stop"])
                self.assertIn("休眠", output)

    def test_an_app_that_cannot_be_asked_is_stopped_by_the_daemon_then_put_to_sleep(self):
        for state in ("starting", "running", "error"):
            with self.subTest(state=state):
                robot = Robot(app={"info": {"name": "mossy"}, "state": state}, page=False)
                code, _, _ = self.stop(robot)
                self.assertEqual(code, 0)
                self.assertTrue(robot.resting)
                self.assertIsNone(robot.app)
                self.assertEqual(robot.mode, "disabled")

    def test_an_app_that_does_not_exit_when_asked_is_stopped_by_the_daemon(self):
        robot = Robot(app={"info": {"name": "mossy"}, "state": "running"}, obeys=False)
        code, _, _ = self.stop(robot)
        self.assertEqual(code, 0)
        self.assertTrue(robot.resting)
        self.assertEqual(robot.posts[:2], ["/api/app/stop", "/api/apps/stop-current-app"])

    def test_an_app_run_in_a_terminal_is_stopped_through_its_page(self):
        robot = Robot(app=None, page=True)  # The daemon manages no app, yet a page answers.
        code, output, _ = self.stop(robot)
        self.assertEqual(code, 0)
        self.assertTrue(robot.resting)
        self.assertEqual(robot.posts, ["/api/app/stop"])
        self.assertIn("已停止", output)

    def test_a_robot_that_cannot_be_put_back_to_sleep_is_reported(self):
        robot = Robot(app={"info": {"name": "mossy"}, "state": "running"}, page=False)
        robot.sleep_fails = True
        code, output, _ = self.stop(robot)
        self.assertEqual(code, 0)  # The app did stop.
        self.assertIsNone(robot.app)
        self.assertIn("HTTP 503", output)

    def test_stop_leaves_other_apps_and_finished_apps_alone(self):
        other = Robot(app={"info": {"name": "another_app"}, "state": "running"})
        code, _, errors = self.stop(other)
        self.assertEqual((code, other.posts), (1, []))
        self.assertIn("another_app", errors)
        for app in (None, {"info": {"name": "mossy"}, "state": "done"}):
            with self.subTest(app=app):
                robot = Robot(app=app, page=False)
                code, output, _ = self.stop(robot)
                self.assertEqual(code, 0)
                self.assertIn("没有在运行", output)
                # Nothing answered on the page's port, and the daemon was not asked to act.
                self.assertEqual(robot.posts, ["/api/app/stop"])
                self.assertEqual((robot.app, robot.asleep, robot.torque), (app, False, True))

    def test_stop_and_end_stop_mossy_and_then_the_daemon_that_mossy_started(self):
        for argv in (("stop",), ("end",), ("stop", "--daemon"), ("end", "--daemon")):
            with self.subTest(argv=argv):
                robot = Robot(app={"info": {"name": "mossy"}, "state": "running"}, owned=True)
                code, output, errors = self.stop(robot, *argv)
                self.assertEqual((code, errors), (0, ""))
                # mossy was asked to exit by itself, so the robot went to sleep
                # once; the simulated kill refuses a signal while it still runs.
                self.assertEqual(robot.posts, ["/api/app/stop"])
                self.assertTrue(robot.resting)
                self.assertEqual(robot.signals, [(4242, signal.SIGTERM)])
                self.assertEqual(robot.processes, {})
                self.assertFalse(robot.recorded)
                self.assertIn("MOSS 已停止", output)
                self.assertIn("后台已退出", output)

    def test_success_is_reported_only_once_the_daemon_process_is_gone(self):
        robot = Robot(app={"info": {"name": "mossy"}, "state": "running"}, owned=True)
        robot.shutdown_seconds = 14.0  # Still within the time it is given.
        code, output, _ = self.stop(robot)
        self.assertEqual(code, 0)
        self.assertGreaterEqual(robot.now[0], 14.0)
        self.assertEqual(robot.processes, {})

    def test_an_owned_daemon_is_shut_down_when_mossy_is_done_or_absent(self):
        done = {"info": {"name": "mossy"}, "state": "done"}
        for app, daemon_state in ((done, "running"), (None, "running"), (done, "stopped")):
            with self.subTest(app=app, daemon=daemon_state):
                robot = Robot(app=app, page=False, owned=True)
                robot.daemon["state"] = daemon_state  # "stopped" still has a live process.
                code, output, _ = self.stop(robot)
                self.assertEqual(code, 0)
                self.assertIn("MOSS 没有在运行", output)
                self.assertEqual(robot.signals, [(4242, signal.SIGTERM)])
                self.assertEqual(robot.processes, {})
                self.assertFalse(robot.recorded)
                self.assertIn("后台已退出", output)
                if daemon_state == "running":
                    # Left upright earlier, the robot is put to sleep by the daemon.
                    self.assertTrue(robot.resting)

    def test_a_silent_daemon_that_mossy_started_is_shut_down_too(self):
        robot = Robot(app=None, page=False, owned=True)
        robot.daemon = None  # Its process lives, but nothing answers on its port.
        code, output, _ = self.stop(robot)
        self.assertEqual(code, 0)
        self.assertEqual((robot.signals, robot.processes), ([(4242, signal.SIGTERM)], {}))

    def test_keep_daemon_stops_only_mossy(self):
        for argv in (("stop", "--keep-daemon"), ("end", "--keep-daemon")):
            with self.subTest(argv=argv):
                robot = Robot(app={"info": {"name": "mossy"}, "state": "running"}, owned=True)
                code, output, _ = self.stop(robot, *argv)
                self.assertEqual(code, 0)
                self.assertTrue(robot.resting)
                self.assertEqual(robot.signals, [])
                self.assertEqual(robot.processes, {4242: DAEMON_COMMAND})
                self.assertTrue(robot.recorded)
                self.assertIn("保留", output)
                self.assertNotIn("后台已退出", output)

    def test_contradictory_daemon_options_are_refused_before_anything_is_touched(self):
        for argv in (("stop", "--daemon", "--keep-daemon"), ("end", "--keep-daemon", "--daemon")):
            with self.subTest(argv=argv):
                robot = Robot(app={"info": {"name": "mossy"}, "state": "running"}, owned=True)
                code, _, errors = self.stop(robot, *argv)
                self.assertEqual(code, 2)
                self.assertIn("not allowed with", errors)  # Both are known; together they clash.
                self.assertEqual((robot.calls, robot.signals), ([], []))
                self.assertTrue(robot.recorded)

    def test_stopping_again_changes_nothing(self):
        robot = Robot(app={"info": {"name": "mossy"}, "state": "running"}, owned=True)
        self.assertEqual(self.stop(robot)[0], 0)
        for command in ("stop", "end"):
            code, output, errors = self.stop(robot, command)
            self.assertEqual((code, errors), (0, ""))
            self.assertIn("MOSS 没有在运行", output)
            self.assertIn("后台没有在运行", output)
        self.assertEqual(robot.signals, [(4242, signal.SIGTERM)])

    def test_a_daemon_that_exits_just_before_the_signal_counts_as_shut_down(self):
        robot = Robot(app=None, page=False, owned=True)
        robot.vanishes = True
        code, output, errors = self.stop(robot)
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual((robot.signals, robot.processes), ([], {}))
        self.assertFalse(robot.recorded)

    def test_a_stale_or_foreign_pid_record_never_leads_to_a_signal(self):
        cases = {
            "the recorded process is gone": ("4242\n", {}),
            "the number now belongs to something else": ("4242\n", {4242: "vim notes.txt"}),
            "the record is not a number": ("soon\n", {4242: DAEMON_COMMAND}),
        }
        for name, (record, processes) in cases.items():
            with self.subTest(name):
                robot = Robot(app=None, page=False)
                robot.record, robot.processes, robot.daemon = record, dict(processes), None
                code, output, errors = self.stop(robot)
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(robot.signals, [])
                self.assertEqual(robot.processes, processes)
                self.assertIn("后台没有在运行", output)

    def test_a_daemon_started_elsewhere_is_left_running_and_reported_as_kept(self):
        for record in (None, "4242\n"):  # No record, or one left by a daemon long gone.
            with self.subTest(record=record):
                robot = Robot(app={"info": {"name": "mossy"}, "state": "running"})
                robot.record = record
                code, output, errors = self.stop(robot)
                self.assertEqual((code, errors), (0, ""))
                self.assertTrue(robot.resting)  # mossy itself still stops cleanly.
                self.assertEqual(robot.signals, [])
                self.assertEqual(robot.processes, {9000: DAEMON_COMMAND})
                self.assertEqual(robot.daemon["state"], "running")
                self.assertIn("不是由 mossy 启动", output)
                self.assertNotIn("后台已退出", output)

    def test_another_robot_app_keeps_itself_and_the_daemon(self):
        for state in ("starting", "running", "stopping", "error"):
            with self.subTest(state=state):
                robot = Robot(app={"info": {"name": "another_app"}, "state": state}, owned=True)
                code, output, errors = self.stop(robot)
                self.assertEqual(code, 1)
                self.assertIn("another_app", errors)
                self.assertEqual((robot.posts, robot.signals), ([], []))
                self.assertEqual(robot.processes, {4242: DAEMON_COMMAND})
                self.assertTrue(robot.recorded)

    def test_an_app_of_another_kind_that_has_finished_does_not_hold_the_daemon_back(self):
        finished = {"info": {"name": "another_app"}, "state": "done"}
        robot = Robot(app=finished, page=False, owned=True)
        code, output, errors = self.stop(robot)
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("MOSS 没有在运行", output)
        self.assertNotIn("/api/apps/stop-current-app", robot.posts)
        self.assertEqual((robot.signals, robot.processes), ([(4242, signal.SIGTERM)], {}))
        self.assertFalse(robot.recorded)

    def test_a_stop_the_daemon_has_already_begun_finishes_before_the_daemon_is_signaled(self):
        # Begun in the dashboard, say. The daemon waits for mossy to exit and then
        # moves the robot; the simulated kill refuses a signal before that ends.
        robot = Robot(app={"info": {"name": "mossy"}, "state": "stopping"}, page=False, owned=True)
        robot.stopped_at = 3.0
        code, output, errors = self.stop(robot)
        self.assertEqual((code, errors), (0, ""))
        self.assertGreaterEqual(robot.now[0], 3.0)
        self.assertNotIn("/api/apps/stop-current-app", robot.posts)  # Not asked twice.
        self.assertEqual((robot.signals, robot.processes), ([(4242, signal.SIGTERM)], {}))
        self.assertTrue(robot.resting)  # The daemon put the robot to sleep on its way out.

    def test_a_stop_that_the_daemon_never_finishes_keeps_the_daemon(self):
        robot = Robot(app={"info": {"name": "mossy"}, "state": "stopping"}, page=False, owned=True)
        code, output, errors = self.stop(robot)
        self.assertEqual(code, 1)
        self.assertIn("MOSS", errors)
        self.assertNotIn("后台已退出", output + errors)
        self.assertEqual((robot.signals, robot.processes), ([], {4242: DAEMON_COMMAND}))
        self.assertTrue(robot.recorded)
        self.assertLess(robot.now[0], 120)  # The wait is bounded.

    def test_a_mossy_that_will_not_exit_keeps_the_daemon_it_depends_on(self):
        robot = Robot(app=None, page=True, obeys=False, owned=True)  # Run in a terminal.
        code, _, errors = self.stop(robot)
        self.assertEqual(code, 1)
        self.assertTrue(errors)
        self.assertEqual(robot.signals, [])
        self.assertTrue(robot.recorded)

    def test_a_daemon_that_does_not_exit_is_a_failure_that_names_what_is_left(self):
        robot = Robot(app={"info": {"name": "mossy"}, "state": "running"}, owned=True)
        robot.shutdown_seconds = None
        code, output, errors = self.stop(robot)
        self.assertEqual(code, 1)
        self.assertIn("4242", errors)
        self.assertIn("仍在运行", errors)
        self.assertNotIn("后台已退出", output + errors)
        self.assertIn("MOSS 已停止", output)  # That part did happen.
        self.assertEqual(robot.signals, [(4242, signal.SIGTERM)])  # Asked once, not killed.
        self.assertEqual(robot.processes, {4242: DAEMON_COMMAND})
        self.assertTrue(robot.recorded)
        self.assertLess(robot.now[0], 120)  # The wait is bounded.

    def test_restart_leaves_the_daemon_running(self):
        robot = Robot(app={"info": {"name": "mossy"}, "state": "running"}, owned=True)
        with patch.object(cli, "request", robot.request):
            with patch.object(cli.os, "kill", side_effect=robot.kill):
                with contextlib.redirect_stdout(io.StringIO()):
                    ready = cli.start("http://127.0.0.1:7863", restart=True)
        self.assertTrue(ready["connected"])
        self.assertEqual(robot.posts, ["/api/apps/restart-current-app"])
        self.assertEqual((robot.signals, robot.processes), ([], {4242: DAEMON_COMMAND}))

    def status(self, connected):
        stats = {
            "reply_ms": 812,
            "reply_ms_p50": 790,
            "reply_ms_p95": 1210,
            "replies": 14,
            "rtt_ms": 182,
            "handshake_ms": 1240,
            "ready_s": 4.72,
            "late_chunks": 0,
            "mic_dropped": 0,
            "reconnects": 2,
            "connected_s": 192,
            "output_rate": 16000,
        }
        return {
            "daemon": {"state": "running", "version": "1.8.4", "error": None},
            "app": {"info": {"name": "mossy"}, "state": "running"},
            "audio_path": "local",
            "page": "http://127.0.0.1:7863/",
            "state": {
                "connected": connected,
                "state": "idle",
                "muted": False,
                "session_id": "0f1e2d3c-4b5a-4978",
                "voice_id": "voice-1",
                "error": None if connected else "InvalidStatus: HTTP 401",
                "stats": stats,
            },
        }

    def test_status_reports_latency_and_exits_nonzero_when_not_connected(self):
        with patch.object(cli, "collect_status", return_value=self.status(True)):
            code, output, _ = self.run_cli("status")
        self.assertEqual(code, 0)
        for expected in ("回复 812 ms", "p95 1210", "往返 182 ms", "启动 4.7 秒", "3m12s"):
            self.assertIn(expected, output)
        self.assertIn("本机直连", output)
        with patch.object(cli, "collect_status", return_value=self.status(False)):
            code, output, _ = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", output)
        with patch.object(cli, "collect_status", return_value=self.status(True)):
            code, output, _ = self.run_cli("status", "--json")
        self.assertEqual(json.loads(output)["state"]["stats"]["rtt_ms"], 182)

    def test_logs_show_the_end_without_noise_and_clean_keeps_the_newest(self):
        with tempfile.TemporaryDirectory() as temp:
            old = Path(temp) / "reachy-daemon-20260101-000000-000000.log"
            new = Path(temp) / "reachy-daemon-20260102-000000-000000.log"
            old.write_text("old\n")
            lines = [f"mossy line {index}" for index in range(100)]
            lines[50] = 'uvicorn.access - INFO - "GET /state HTTP/1.1" 200'
            lines[98] = "\x1b[2mgst_plugin_webrtc_signalling\x1b[0m noise"
            new.write_text("\n".join(lines) + "\n")
            with patch.object(cli, "STATE", Path(temp)):
                shown = self.run_cli("logs", "-n", "3")[1].splitlines()
                self.assertEqual(shown, ["mossy line 96", "mossy line 97", "mossy line 99"])
                shown = self.run_cli("logs", "-n", "2", "--all")[1].splitlines()
                self.assertEqual(shown, ["gst_plugin_webrtc_signalling noise", "mossy line 99"])
                self.run_cli("logs", "--clean")
            self.assertEqual([path.name for path in Path(temp).iterdir()], [new.name])


class ConfigCommandTests(CommandTests):
    def setUp(self):
        super().setUp()
        from mossy import config

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.env = Path(directory.name) / ".env"
        patcher = patch.object(config, "USER_ENV", self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_settings_are_saved_to_the_file_when_the_app_is_not_running(self):
        with patch.object(cli, "request", return_value=None):
            code, _, _ = self.run_cli("config", "set", "live_speed=1.2", "MOSS_POSTURE=curious")
        self.assertEqual(code, 0)
        self.assertIn("MOSS_LIVE_SPEED", self.env.read_text())
        self.assertIn("curious", self.env.read_text())
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.run_cli("config", "unset", "posture")
        self.assertNotIn("MOSS_POSTURE", self.env.read_text())

    def test_invalid_names_and_values_change_nothing(self):
        for argument in ("MOSS_LIVE_SPEED=9", "MOSS_NOT_A_SETTING=1", "MOSS_POSTURE"):
            with self.subTest(argument=argument):
                with patch.object(cli, "request", return_value=None):
                    code, _, errors = self.run_cli("config", "set", argument)
                self.assertEqual(code, 1)
                self.assertTrue(errors)
        self.assertFalse(self.env.exists())

    def test_a_running_app_applies_the_change_and_reports_its_refusal(self):
        received = []
        answers = [(200, {"saved": ["MOSS_LIVE_SPEED"], "restart": False})]

        class Page(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond(200, {"connected": True})

            def do_POST(self):
                size = int(self.headers["Content-Length"])
                received.append((self.path, json.loads(self.rfile.read(size))))
                self.respond(*answers[0])

            def respond(self, status, body):
                body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with patch.dict(os.environ, {"MOSS_WEB_UI_PORT": str(server.server_address[1])}):
            code, output, _ = self.run_cli("config", "set", "MOSS_LIVE_SPEED=1.2")
            self.assertEqual(code, 0)
            self.assertEqual(received, [("/api/settings", {"MOSS_LIVE_SPEED": 1.2})])
            self.assertIn("MOSS_LIVE_SPEED", output)
            self.assertFalse(self.env.exists())  # The app saves what it applied.

            answers[0] = (502, {"error": "Requested voice x, server confirmed None"})
            code, _, errors = self.run_cli("config", "set", "MOSS_LIVE_VOICE_ID=x")
            self.assertEqual(code, 1)
            self.assertIn("server confirmed", errors)

    def test_listing_settings_never_prints_the_api_key(self):
        secret = "sk-" + "b" * 40
        with patch.dict(os.environ, {"MOSS_API_KEY": secret, "MOSS_LIVE_SPEED": "1.3"}):
            code, output, _ = self.run_cli("config")
            self.assertEqual(code, 0)
            self.assertNotIn(secret, output)
            self.assertIn("已设置", output)
            self.assertIn("* MOSS_LIVE_SPEED", output)
            self.assertEqual(self.run_cli("config", "get", "MOSS_LIVE_SPEED")[1].strip(), "1.3")
            code, output, errors = self.run_cli("config", "get", "MOSS_API_KEY")
            self.assertEqual(code, 1)
            self.assertNotIn(secret, output + errors)


class PackagingTests(unittest.TestCase):
    def test_the_command_starts_with_the_standard_library_alone(self):
        # -S hides site-packages, as on a machine where nothing is installed yet.
        script = "import sys; sys.path.insert(0, 'src'); import mossy.cli as c; c.main(['help'])"
        result = subprocess.run(
            [sys.executable, "-I", "-S", "-c", script], cwd=ROOT, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mossy install", result.stdout)

    def test_finder_launcher_runs_the_start_command_from_this_checkout(self):
        launcher = ROOT / "start-mossy.command"
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertEqual(subprocess.run(["/bin/bash", "-n", str(launcher)]).returncode, 0)
        text = launcher.read_text()
        self.assertIn("-m mossy.cli start", text)
        self.assertNotIn("MOSS_API_KEY=", text)

    def install(self, home, *argv):
        """Run ``mossy install`` with a stand-in for Reachy's runtime and for uv."""
        runtime = home / "runtime"
        (runtime / "apps_venv/bin").mkdir(parents=True, exist_ok=True)
        (runtime / "apps_venv/bin/python").touch()
        (runtime / "uv").touch()
        commands = []

        def run(command, **kwargs):
            commands.append([str(part) for part in command])
            return Mock(returncode=0)

        output = io.StringIO()
        with (
            patch.object(cli, "RUNTIME", runtime),
            patch.object(cli.Path, "home", return_value=home),
        ):
            with patch.object(cli.subprocess, "run", side_effect=run):
                with patch.object(cli, "installed_app", return_value={"port": 9}):
                    with patch.object(cli, "request", return_value=None):
                        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                            code = cli.main(["install", *argv])
        return code, commands, runtime

    def test_install_targets_reachys_app_environment_and_links_the_command(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            code, commands, runtime = self.install(home, "--editable")
            self.assertEqual(code, 0)
            python = str(runtime / "apps_venv/bin/python")
            self.assertEqual(
                commands[0],
                [
                    str(runtime / "uv"),
                    "pip",
                    "install",
                    "--python",
                    python,
                    "--editable",
                    str(ROOT),
                ],
            )
            self.assertEqual(commands[1][0], python)  # Recordings are fetched in that environment.
            link = home / ".local/bin/mossy"
            self.assertEqual(os.readlink(link), str(runtime / "apps_venv/bin/mossy"))
            # A second install refreshes the link; a real file there is left alone.
            self.assertEqual(self.install(home)[0], 0)
            self.assertTrue(link.is_symlink())
            link.unlink()
            link.write_text("#!/bin/sh\n")
            self.assertEqual(self.install(home)[0], 0)
            self.assertEqual(link.read_text(), "#!/bin/sh\n")

    def test_install_refuses_a_directory_that_is_not_the_source(self):
        with tempfile.TemporaryDirectory() as temp:
            code, commands, _ = self.install(Path(temp), "--source", temp)
        self.assertEqual((code, commands), (1, []))

    def test_fingerprint_follows_the_code_and_the_page(self):
        with tempfile.TemporaryDirectory() as temp:
            package = Path(temp)
            (package / "static").mkdir()
            (package / "main.py").write_text("print(1)\n")
            (package / "static" / "main.js").write_text("let a = 1;\n")
            before = cli.fingerprint(package)
            self.assertEqual(before, cli.fingerprint(package))
            (package / "static" / "main.js").write_text("let a = 2;\n")
            self.assertNotEqual(before, cli.fingerprint(package))


if __name__ == "__main__":
    unittest.main()
