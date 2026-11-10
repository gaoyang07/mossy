"""Exercise the standalone Finder launcher's service decisions without hardware."""

import contextlib
import io
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def load_launcher():
    path = Path(__file__).resolve().parents[1] / "start-mossy.command"
    source = path.read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
    module = types.ModuleType("mossy_launcher_test")
    exec(compile(source, str(path), "exec"), module.__dict__)
    return module


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


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.launcher = load_launcher()
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
                with patch.object(
                    self.launcher.sys, "argv", ["start-mossy.command", "--no-browser"]
                ):
                    with patch.object(self.launcher.fcntl, "flock", side_effect=BlockingIOError):
                        with patch.object(self.launcher, "start") as start:
                            with contextlib.redirect_stdout(io.StringIO()):
                                self.assertEqual(self.launcher.main(), 0)
                            start.assert_not_called()

    def test_error_messages_redact_api_keys(self):
        secret = "sk-" + "a" * 48
        self.assertNotIn(secret, self.launcher.safe_message("error: " + secret))


if __name__ == "__main__":
    unittest.main()
