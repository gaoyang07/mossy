"""The ``mossy`` command: start, stop, inspect and configure the robot conversation.

This module needs only the standard library, so it can run from a bare checkout
with any Python and install the app. Commands that read settings or talk to the
MOSS platform import the rest of the package when they run.

The Reachy daemon owns the robot and runs mossy as one of its apps, so starting
goes through the daemon's local HTTP API. Stopping asks mossy itself to exit,
then terminates the daemon if ``mossy start`` was what launched it.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

RUNTIME = Path.home() / "Library/Application Support/com.pollen-robotics.reachy-mini"
STATE = Path.home() / ".local/state/mossy"
DAEMON = "http://127.0.0.1:8000"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
STARTUP_TIMEOUT = 180  # A first GStreamer registry scan can take over a minute.
STOP_TIMEOUT = 40  # Going to sleep takes the app about five seconds.
DAEMON_STOP_TIMEOUT = 30  # The daemon's sleep routine comes first: about six seconds.
POLL_SECONDS = 0.2
# reachy_mini.daemon.utils.CAMERA_SOCKET_PATH. The SDK plays and records on the
# audio device directly only while the daemon offers this camera socket; without
# it both directions take a WebRTC detour through the daemon.
CAMERA_SOCKET = Path("/tmp/reachymini_camera_socket")
LOG_NOISE = ("gst_plugin_webrtc_signalling", "uvicorn.access", "[Central Relay]")


class StartupError(Exception):
    pass


def safe_message(value: object) -> str:
    return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", str(value))[:400]


def request(route: str, *, base: str = DAEMON, method: str = "GET", optional: bool = False) -> Any:
    req = urllib.request.Request(base + route, method=method)
    try:
        # Stopping an app waits for the robot to go to sleep.
        with HTTP.open(req, timeout=45 if method == "POST" else 2) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise StartupError(f"接口 {route} 返回 HTTP {exc.code}。") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        if optional:
            return None
        raise StartupError(f"无法连接 {base}，请检查 Reachy 后台。") from exc
    except (ValueError, UnicodeError) as exc:
        raise StartupError(f"{base} 未返回有效的 Reachy/MOSS 状态。") from exc


def direct_environment() -> dict[str, str]:
    env = os.environ.copy()
    # This command may itself run in one of Reachy's Pythons, whose .pth
    # bootstrap has already populated these values. A child Python bootstraps
    # again, even prepending to scalar scanner/registry paths and making them
    # invalid. Follow reachy_mini.apps.manager's child-env isolation; also
    # prevent the config probe in apps_venv from importing another
    # environment's packages.
    for name in (
        "GST_PLUGIN_PATH_1_0",
        "GST_PLUGIN_SYSTEM_PATH_1_0",
        "GST_REGISTRY_1_0",
        "GST_PLUGIN_SCANNER_1_0",
        "GST_PYTHONPATH_1_0",
        "GI_TYPELIB_PATH",
        "PYGI_DLL_DIRS",
        "XDG_DATA_DIRS",
        "XDG_CONFIG_DIRS",
        "PYTHONPATH",
        "PYTHONHOME",
    ):
        env.pop(name, None)
    env["PYTHONIOENCODING"] = "utf-8"
    for name in ("NO_PROXY", "no_proxy"):
        hosts = [v for v in env.get(name, "").split(",") if v]
        for host in ("localhost", "127.0.0.1", "::1"):
            if host not in hosts:
                hosts.append(host)
        env[name] = ",".join(hosts)
    return env


def fingerprint(package: str | Path) -> str:
    """A digest of a mossy package directory's code and page."""
    digest = hashlib.sha256()
    for path in sorted(Path(package).glob("*.py")) + sorted(Path(package).glob("static/*")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def installed_app() -> dict[str, Any]:
    """What the app will see when the daemon starts it: its settings and its code."""
    python = RUNTIME / "apps_venv/bin/python"
    if not python.exists():
        raise StartupError("找不到 Reachy 应用环境，请先打开一次 Reachy Mini Control 完成初始化。")
    # Print only readiness and port, never the key itself.
    probe = (
        "import json, os, mossy; from mossy.config import load; s = load(); "
        "print(json.dumps({'configured': bool(s['MOSS_API_KEY']), "
        "'port': s['MOSS_WEB_UI_PORT'], 'package': os.path.dirname(mossy.__file__)}))"
    )
    result = subprocess.run(
        [str(python), "-c", probe],
        cwd=RUNTIME,
        env=direct_environment(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode:
        raise StartupError(
            "无法读取 mossy 配置；请运行 mossy install 把当前版本安装到 Reachy 的 apps_venv。"
        )
    return json.loads(result.stdout)


def settings() -> str:
    data = installed_app()
    if not data["configured"]:
        raise StartupError(
            "还没有 API key；请运行 mossy config set MOSS_API_KEY=你的密钥"
            "（保存在 ~/.config/mossy/.env）。"
        )
    if not isinstance(data["port"], int) or not 1 <= data["port"] <= 65535:
        raise StartupError("MOSS_WEB_UI_PORT 不是有效端口。")
    here = Path(__file__).resolve().parent
    if Path(data["package"]).resolve() != here and fingerprint(data["package"]) != fingerprint(
        here
    ):
        print(
            "注意：Reachy 的 apps_venv 里是另一份 mossy 代码；运行 mossy install 同步。", flush=True
        )
    return f"http://127.0.0.1:{data['port']}"


def spawn_daemon() -> subprocess.Popen:
    # A separate session and file-backed output let this terminal close safely.
    log = STATE / f"reachy-daemon-{datetime.now():%Y%m%d-%H%M%S-%f}.log"
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "ab", buffering=0) as output:
        process = subprocess.Popen(
            [
                str(RUNTIME / ".venv/bin/python3"),
                "-m",
                "reachy_mini.daemon.app.main",
                "--desktop-app-daemon",
                "--no-wake-up-on-start",
                "--preload-datasets",
            ],
            cwd=RUNTIME,
            env=direct_environment(),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
    (STATE / "reachy-daemon.pid").write_text(f"{process.pid}\n")
    print(f"正在启动 Reachy 后台。日志：{log}", flush=True)
    process.mossy_log = log
    return process


def wait_daemon(process: subprocess.Popen | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    next_notice = time.monotonic() + 15
    while time.monotonic() < deadline:
        data = request("/api/daemon/status", optional=True)
        if data and data.get("state") in ("running", "stopped", "error"):
            return data
        if process is not None and process.poll() is not None:
            log = getattr(process, "mossy_log", STATE)
            raise StartupError(f"Reachy 后台提前退出（退出码 {process.returncode}），日志：{log}")
        if time.monotonic() >= next_notice:
            print("等待 Reachy 后台初始化，首次媒体插件扫描可能较慢…", flush=True)
            next_notice = time.monotonic() + 15
        time.sleep(POLL_SECONDS)
    raise StartupError(f"等待 Reachy 后台超过 {STARTUP_TIMEOUT} 秒，请查看 {STATE} 中最新的日志。")


def run_job(route: str) -> None:
    job = request(route, method="POST")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = request("/api/apps/job-status/" + job["job_id"])
        if result["status"] == "done":
            return
        if result["status"] == "error":
            raise StartupError("Reachy 后台恢复失败，请检查机器人电源和 USB 连接。")
        time.sleep(POLL_SECONDS)
    raise StartupError("Reachy 后台恢复超时，请在控制台检查状态。")


def current_app() -> dict[str, Any] | None:
    current = request("/api/apps/current-app-status")
    if current and current["info"]["name"] != "mossy":
        name = safe_message(current["info"]["name"])
        raise StartupError(f"当前应用是 {name}，请先在 Reachy 控制台停止它，再启动 MOSS。")
    return current


def wait_connected(base: str, seconds: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + seconds
    next_notice = time.monotonic() + 15
    next_check = time.monotonic()
    reported = None
    while time.monotonic() < deadline:
        data = request("/state", base=base, optional=True)
        if data and data.get("connected"):
            return data
        if data and data.get("error") and data["error"] != reported:
            reported = data["error"]
            print("云端连接暂未成功，继续重试：" + safe_message(reported), flush=True)
        if time.monotonic() >= next_check:
            # The page is polled often; the daemon only needs an occasional look.
            next_check = time.monotonic() + 1
            current = current_app()
            if current and current.get("state") == "error":
                raise StartupError("MOSS 启动失败：" + safe_message(current.get("error", "")))
            daemon = request("/api/daemon/status")
            if daemon.get("state") == "error":
                raise StartupError("机器人连接异常：" + safe_message(daemon.get("error", "")))
        if time.monotonic() >= next_notice:
            print("等待 MOSS 初始化媒体并连接云端…", flush=True)
            next_notice = time.monotonic() + 15
        time.sleep(POLL_SECONDS)
    return None


def start(base: str, restart: bool = False) -> dict[str, Any]:
    daemon = request("/api/daemon/status", optional=True)
    spawned = False
    if daemon is None:
        try:
            with socket.create_connection(("127.0.0.1", 8000), timeout=0.5):
                raise StartupError("8000 端口已被占用，但 Reachy 状态不可用；请检查已有服务。")
        except OSError:
            pass
        daemon = wait_daemon(spawn_daemon())
        spawned = True
    elif daemon.get("state") not in ("running", "stopped", "not_initialized", "error"):
        daemon = wait_daemon()

    current = current_app()
    if daemon["state"] in ("error", "stopped", "not_initialized") and not spawned:
        print("正在恢复机器人连接…", flush=True)
        if current:
            request("/api/apps/stop-current-app", method="POST")
        route = (
            "/api/daemon/restart"
            if daemon["state"] == "error"
            else "/api/daemon/start?wake_up=false"
        )
        run_job(route)
        daemon = wait_daemon()
    if daemon["state"] != "running":
        detail = safe_message(daemon.get("error") or daemon["state"])
        raise StartupError(f"机器人尚未就绪：{detail}\n请确认独立电源、底部开关和 USB 连接。")

    current = current_app()
    if current and current["state"] == "stopping":
        raise StartupError("MOSS 正在停止，请等待几秒后再运行。")
    if current and current["state"] in ("running", "starting") and not restart:
        ready = wait_connected(base, 8)
        if ready:
            print("MOSS 已经在运行，直接使用现有连接。", flush=True)
            return ready
    print("正在启动 MOSS…", flush=True)
    # An app that ended on its own is still listed, but only a live one restarts.
    finished = current and current["state"] == "done"
    route = (
        "/api/apps/restart-current-app" if current and not finished else "/api/apps/start-app/mossy"
    )
    request(route, method="POST")
    try:
        ready = wait_connected(base, STARTUP_TIMEOUT)
        if ready is None:
            raise StartupError("MOSS 尚未连上云端，请检查网络和 API key，然后重试。")
        return ready
    except StartupError:
        # Roll back only the MOSS start/restart performed by this invocation.
        try:
            if current_app():
                request("/api/apps/stop-current-app", method="POST")
        except StartupError:
            pass
        raise


# --- commands ---------------------------------------------------------------


def cmd_start(args: argparse.Namespace) -> int:
    """Start the Reachy daemon and mossy, or reuse a healthy running conversation."""
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(STATE / "start.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有启动流程正在执行，请稍等。")
            return 0
        began = time.monotonic()
        base = settings()
        ready = start(base, restart=args.restart)
        print(f"MOSS 已就绪：{base}/（用时 {time.monotonic() - began:.1f} 秒）", flush=True)
        print("全双工：" + ("开启" if ready.get("full_duplex") else "关闭"), flush=True)
        expression = ready.get("expressions") or {}
        if expression.get("enabled") and expression.get("available"):
            print("表情动作：开启", flush=True)
        elif expression.get("error"):
            print("语音已连接；表情暂不可用，详情见状态页。", flush=True)
        camera = ready.get("camera") or {}
        if camera.get("enabled"):
            if camera.get("available"):
                print("Reachy 摄像头：已有画面", flush=True)
            else:
                print(
                    "视觉已开启，正在等待 Reachy 摄像头画面；请在状态页检查连接及相机权限。",
                    flush=True,
                )
        if not CAMERA_SOCKET.exists():
            print(
                "提示：Reachy 后台启动时没有找到机器人摄像头，音频改走后台的 WebRTC 中转"
                "（多一次编解码，打断也更慢）。机器人通电后运行 "
                "mossy stop && mossy start 可恢复直连。",
                flush=True,
            )
        if not args.no_browser:
            subprocess.run(["/usr/bin/open", base + "/" + getattr(args, "page", "")], check=False)
        print("服务在后台运行，可以关闭此终端窗口；停止请运行 mossy stop。", flush=True)
        return 0


def cmd_gui(args: argparse.Namespace) -> int:
    """Open the control page, starting mossy first if it is not running."""
    args.restart, args.no_browser, args.page = False, False, "#settings"
    return cmd_start(args)


def daemon_pid() -> int | None:
    """The daemon this command started, if it is still that process."""
    try:
        pid = int((STATE / "reachy-daemon.pid").read_text())
    except (OSError, ValueError):
        return None
    listing = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
    )
    return pid if "reachy_mini.daemon.app.main" in listing.stdout else None


def sleep_robot() -> None:
    """Put the robot to sleep through the daemon and release its motors.

    This is the daemon's own sequence when it stops. Asking for "enabled" first
    matters: the daemon can turn the motors on without updating the mode it
    reports, and it ignores a request for the mode it already reports.
    """
    request("/api/motors/set_mode/enabled", method="POST")
    move = request("/api/move/play/goto_sleep", method="POST")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if all(running["uuid"] != move["uuid"] for running in request("/api/move/running")):
            break
        time.sleep(POLL_SECONDS)
    request("/api/motors/set_mode/disabled", method="POST")


def stop_app() -> bool:
    """Stop mossy and leave the robot asleep; False if it was not running."""
    current = request("/api/apps/current-app-status", optional=True)
    if current and current["info"]["name"] == "mossy" and current["state"] == "stopping":
        # Begun elsewhere, in the daemon's dashboard for one. The daemon is waiting
        # for mossy to exit and then moves the robot; mossy runs until that is over.
        print("Reachy 后台正在停止 MOSS，等它完成…", flush=True)
        deadline = time.monotonic() + STOP_TIMEOUT
        while current and current["state"] == "stopping":
            if time.monotonic() >= deadline:
                raise StartupError(
                    f"Reachy 后台没有在 {STOP_TIMEOUT} 秒内停下 MOSS；没有做任何改动。"
                )
            time.sleep(POLL_SECONDS)
            current = request("/api/apps/current-app-status", optional=True)
    # An app that ran to its end is still listed, but nothing of it is left to disturb.
    if current and current["info"]["name"] != "mossy" and current["state"] != "done":
        name = safe_message(current["info"]["name"])
        raise StartupError(f"正在运行的是 {name}，不是 MOSS；没有做任何改动。")
    managed = bool(current) and current["state"] != "done"
    # Asked to stop an app, the daemon waits for it to exit and then moves the
    # robot back to its zero pose with the motors on. mossy puts the robot to
    # sleep on its own way out, so ask it through its page instead; the daemon
    # leaves the robot of an app that ended by itself alone.
    try:
        page = f"http://127.0.0.1:{installed_app()['port']}"
        asked = request("/api/app/stop", base=page, method="POST", optional=True) is not None
    except (StartupError, subprocess.TimeoutExpired, ValueError):
        asked = False  # Not installed there, or an app from before the page could stop it.
    if not asked and not managed:
        return False
    print("正在停止 MOSS，机器人会回到休眠姿态…", flush=True)
    deadline = time.monotonic() + STOP_TIMEOUT
    while asked and time.monotonic() < deadline:
        current = request("/api/apps/current-app-status", optional=True) if managed else None
        exited = not current or current["state"] in ("done", "error")
        if exited and request("/state", base=page, optional=True) is None:
            return True
        time.sleep(POLL_SECONDS)
    if not managed:
        raise StartupError(f"MOSS 没有在 {STOP_TIMEOUT} 秒内退出；请在运行它的终端里按 Ctrl+C。")
    print("MOSS 没有自己退出，改由 Reachy 后台停止，再让机器人重新休眠…", flush=True)
    request("/api/apps/stop-current-app", method="POST")
    try:
        sleep_robot()
    except StartupError as exc:
        print("没能让机器人回到休眠姿态：" + safe_message(exc), flush=True)
    return True


def stop_daemon(pid: int) -> None:
    """Terminate the daemon that ``mossy start`` launched and wait until it is gone.

    SIGTERM is the daemon's own graceful shutdown: it releases the media devices,
    runs its sleep routine (the chime and a five-second wait even when the robot
    already sleeps), releases the motors and exits. Its HTTP API reporting
    "stopped" is not that; the process and its listener outlive it.
    """
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass  # It exited between being found and being signaled.
    deadline = time.monotonic() + DAEMON_STOP_TIMEOUT
    while daemon_pid() is not None:
        if time.monotonic() >= deadline:
            raise StartupError(
                f"Reachy 后台（进程 {pid}）在 {DAEMON_STOP_TIMEOUT} 秒内没有退出，仍在运行；"
                "可以稍后再运行 mossy stop，或用 mossy logs 查看它停在哪里。"
            )
        time.sleep(POLL_SECONDS)
    (STATE / "reachy-daemon.pid").unlink(missing_ok=True)


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop mossy, then shut down the Reachy daemon if ``mossy start`` launched it."""
    if stop_app():
        print("MOSS 已停止，云端会话已关闭。", flush=True)
    else:
        print("MOSS 没有在运行。", flush=True)
    # Only now: mossy needs the daemon until it has put the robot to sleep, and
    # stop_app() raises rather than returns while any app is still running.
    pid = daemon_pid()
    if pid is None:
        if request("/api/daemon/status", optional=True) is None:
            print("Reachy 后台没有在运行。", flush=True)
        else:
            print(
                "Reachy 后台不是由 mossy 启动的，保持运行；"
                "请在启动它的程序（例如 Reachy Mini Control）里关闭。",
                flush=True,
            )
    elif args.keep_daemon:
        print(
            f"按 --keep-daemon 保留了 Reachy 后台（进程 {pid}）；"
            "它继续持有机器人的媒体设备，mossy stop 可以关闭它。",
            flush=True,
        )
    else:
        print(
            f"正在关闭 Reachy 后台（进程 {pid}）并等它退出；它还连着机器人时会先执行"
            "自己的休眠流程，提示音会再响一次，约需 6 秒…",
            flush=True,
        )
        stop_daemon(pid)
        print("Reachy 后台已退出，不再占用机器人的媒体设备。", flush=True)
    return 0


def collect_status() -> dict[str, Any]:
    daemon = request("/api/daemon/status", optional=True)
    status = {"daemon": daemon, "app": None, "state": None, "page": None}
    if daemon is not None:
        status["app"] = request("/api/apps/current-app-status", optional=True)
        status["audio_path"] = "local" if CAMERA_SOCKET.exists() else "webrtc"
    try:
        port = installed_app()["port"]
    except (StartupError, subprocess.TimeoutExpired, ValueError):
        return status
    status["page"] = f"http://127.0.0.1:{port}/"
    state = request("/state", base=status["page"].rstrip("/"), optional=True)
    if state is not None:
        state.pop("transcript", None)
        status["state"] = state
    return status


def duration(seconds: float) -> str:
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m" if hours else f"{minutes}m{seconds:02d}s"


def cmd_status(args: argparse.Namespace) -> int:
    """Show the daemon, the app, the MOSS-Live session and measured latency."""
    status = collect_status()
    state = status["state"]
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if state and state.get("connected") else 1

    def row(label: str, text: str) -> None:
        print(f"{label:<11}{text}")

    daemon, app = status["daemon"], status["app"]
    if daemon is None:
        row("Reachy 后台", "未运行（mossy start 会启动它）")
    else:
        detail = f"{daemon.get('state')} · v{daemon.get('version') or '?'} · {DAEMON}"
        if daemon.get("error"):
            detail += " · " + safe_message(daemon["error"])
        row("Reachy 后台", detail)
        path = "本机直连" if status["audio_path"] == "local" else "后台 WebRTC 中转（延迟更高）"
        row("音频通路", path)
        row("应用", f"{app['info']['name']} · {app['state']}" if app else "没有应用在运行")
    if state is None:
        row("MOSS", "未运行")
        return 1
    stats = state.get("stats") or {}
    if state.get("connected"):
        session = (state.get("session_id") or "")[:8]
        voice = state.get("voice_id") or "默认音色"
        row("MOSS-Live", f"已连接 · 会话 {session}… · 音色 {voice} · {stats.get('output_rate')} Hz")
        connected = duration(stats.get("connected_s") or 0)
        row("", f"本次连接 {connected} · 重连 {stats.get('reconnects')} 次")
    else:
        row("MOSS-Live", "未连接 · " + safe_message(state.get("error") or state.get("state")))
    parts = []
    if stats.get("reply_ms") is not None:
        parts.append(
            f"回复 {stats['reply_ms']} ms（中位 {stats['reply_ms_p50']} / p95 "
            f"{stats['reply_ms_p95']}，共 {stats['replies']} 次）"
        )
    for label, key, unit in (
        ("往返", "rtt_ms", " ms"),
        ("握手", "handshake_ms", " ms"),
        ("启动", "ready_s", " 秒"),
    ):
        if stats.get(key) is not None:
            value = f"{stats[key]:.1f}" if key == "ready_s" else stats[key]
            parts.append(f"{label} {value}{unit}")
    if stats.get("late_chunks"):
        parts.append(f"语音断续 {stats['late_chunks']} 次（最长 {stats['late_ms_max']} ms）")
    if stats.get("mic_dropped"):
        parts.append(f"麦克风丢弃 {stats['mic_dropped']} 块")
    if parts:
        row("延迟", " · ".join(parts))
    row("状态", state.get("state") + ("（麦克风已静音）" if state.get("muted") else ""))
    row("页面", status["page"])
    return 0 if state.get("connected") else 1


def newest_log() -> Path:
    logs = sorted(STATE.glob("reachy-daemon-*.log"))
    if not logs:
        raise StartupError(
            f"{STATE} 里没有日志；后台如果由 Reachy Mini Control 启动，请在它的界面里查看。"
        )
    return logs[-1]


def cmd_logs(args: argparse.Namespace) -> int:
    """Print the daemon log, which includes everything mossy logs."""
    log = newest_log()
    if args.clean:
        removed = [path for path in STATE.glob("reachy-daemon*.log") if path != log]
        size = sum(path.stat().st_size for path in removed)
        for path in removed:
            path.unlink()
        print(f"已删除 {len(removed)} 个旧日志（{size / 1e6:.1f} MB），保留 {log.name}。")
        return 0

    def show(text: str) -> None:
        for line in text.splitlines():
            line = re.sub(r"\x1b\[[0-9;]*m", "", line)
            if args.all or not any(noise in line for noise in LOG_NOISE):
                print(line[:2000])

    with open(log, "rb") as stream:
        # Logs reach tens of megabytes; the end is all that is needed.
        stream.seek(max(0, log.stat().st_size - 400_000))
        tail = stream.read().decode("utf-8", "replace").splitlines()
        if not args.all:
            tail = [line for line in tail if not any(noise in line for noise in LOG_NOISE)]
        show("\n".join(tail[-args.lines :]))
        if not args.follow:
            return 0
        try:
            while True:
                chunk = stream.read()
                if chunk:
                    show(chunk.decode("utf-8", "replace"))
                    sys.stdout.flush()
                else:
                    time.sleep(0.25)
        except KeyboardInterrupt:
            return 0


def setting_key(name: str) -> str:
    from mossy import config

    key = name.strip().upper()
    for candidate in (key, "MOSS_" + key):
        if candidate in config.FIELDS:
            return candidate
    raise StartupError(f"没有名为 {name} 的设置；运行 mossy config 查看全部。")


def apply_settings(values: dict[str, Any]) -> None:
    """Change settings through the running app when there is one, else in the file."""
    from mossy import config

    try:
        values = {key: config.coerce(key, value) for key, value in values.items()}
    except ValueError as exc:
        raise StartupError(str(exc)) from exc
    page = f"http://127.0.0.1:{config.load()['MOSS_WEB_UI_PORT']}"
    if request("/state", base=page, optional=True) is None:
        config.save(values)
        print(f"已保存到 {config.USER_ENV}，下次 mossy start 生效。")
        return
    req = urllib.request.Request(
        page + "/api/settings",
        data=json.dumps(values).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with HTTP.open(req, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = json.loads(exc.read() or b"{}").get("error", f"HTTP {exc.code}")
        raise StartupError(f"没有应用：{detail}") from exc
    print("已应用并保存：" + ("、".join(result["saved"]) or "没有变化"))
    if result["restart"]:
        print("其中有设置要到下次启动才生效：mossy restart")


def cmd_config(args: argparse.Namespace) -> int:
    """Show or change settings in ~/.config/mossy/.env."""
    from mossy import config

    action, items = args.action, args.items
    if action == "path":
        print(config.USER_ENV)
    elif action == "edit":
        config.save({})  # Creates the directory with private permissions.
        config.USER_ENV.touch(mode=0o600)
        editor = os.environ.get("EDITOR", "/usr/bin/open -t").split()
        subprocess.run([*editor, str(config.USER_ENV)], check=False)
    elif action == "get":
        for name in items:
            key = setting_key(name)
            if config.FIELDS[key].kind == "secret":
                raise StartupError(f"{key} 不会显示；它保存在 {config.USER_ENV}。")
            print(config.as_text(key, config.load()[key]))
    elif action == "set":
        if not items or any("=" not in item for item in items):
            raise StartupError("用法：mossy config set 名称=值 [名称=值 …]")
        apply_settings({setting_key(k): v for k, v in (item.split("=", 1) for item in items)})
    elif action == "unset":
        config.save({setting_key(name): None for name in items})
        print("已恢复默认值，下次 mossy start（或 mossy restart）生效。")
    else:
        described = config.describe(config.load())
        for group, title in config.GROUPS.items():
            print(f"\n{title}")
            for item in (item for item in described if item["group"] == group):
                if item["kind"] == "secret":
                    value = "已设置" if item["is_set"] else "未设置"
                else:
                    value = config.as_text(item["key"], item["value"]).replace("\n", "⏎")
                    value = value if len(value) <= 48 else value[:47] + "…"
                changed = item["kind"] != "secret" and item["value"] != item["default"]
                note = "（由环境变量固定）" if item["pinned"] else ""
                mark = "*" if changed else " "
                print(f"  {mark} {item['key']:<28}{value:<22} {item['label']}{note}")
        print(f"\n* 表示不是默认值。文件：{config.USER_ENV}")
    return 0


def cmd_voices(args: argparse.Namespace) -> int:
    """List the account's voices, or switch to one of them."""
    from mossy import config
    from mossy.live_client import list_voices

    current = config.load()
    if not current["MOSS_API_KEY"]:
        raise StartupError("还没有 API key；请先运行 mossy config set MOSS_API_KEY=你的密钥。")
    try:
        voices = list_voices(
            current["MOSS_LIVE_URL"], current["MOSS_API_KEY"], current["MOSS_LIVE_VERIFY_TLS"]
        )
    except (OSError, ValueError) as exc:
        raise StartupError(f"无法读取音色列表：{exc}") from exc
    if args.use:
        matches = [voice for voice in voices if args.use in (voice["id"], voice["name"])]
        if len(matches) > 1:
            raise StartupError(f"有 {len(matches)} 个音色叫 {args.use}，请改用 ID。")
        # An ID that is not in the account's list may still be a platform preset.
        apply_settings({"MOSS_LIVE_VOICE_ID": matches[0]["id"] if matches else args.use})
        return 0
    if not voices:
        print("账号下还没有自己的音色；也可以直接使用平台预置音色的 ID。")
    for voice in voices:
        created = voice["created_at"]
        day = datetime.fromtimestamp(created).strftime("%Y-%m-%d") if created else ""
        mark = "*" if voice["id"] == current["MOSS_LIVE_VOICE_ID"] else " "
        print(f"{mark} {voice['id']}  {day:<10}  {voice['name']}")
    print("\n切换音色：mossy voices --use <ID 或名字>；* 是当前音色。")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Install this code into Reachy's app environment and link the ``mossy`` command."""
    source = Path(args.source or Path(__file__).resolve().parents[2]).resolve()
    if not (source / "pyproject.toml").exists():
        raise StartupError(f"{source} 不是 mossy 源码目录；请用 --source 指定。")
    python = RUNTIME / "apps_venv/bin/python"
    if not python.exists():
        raise StartupError("找不到 Reachy 应用环境，请先打开一次 Reachy Mini Control 完成初始化。")
    uv = RUNTIME / "uv" if (RUNTIME / "uv").exists() else shutil.which("uv")
    if not uv:
        raise StartupError("找不到 uv；请先安装 https://docs.astral.sh/uv/ 。")
    command = [str(uv), "pip", "install", "--python", str(python)]
    command += ["--editable", str(source)] if args.editable else [str(source)]
    print("正在安装：" + " ".join(command), flush=True)
    if subprocess.run(command, env=direct_environment()).returncode:
        raise StartupError("安装失败，详情见上面的输出。")
    # Gestures play recordings from the Hugging Face cache and never download
    # during a conversation, so fetch the pinned revisions now.
    fetch = (
        "from huggingface_hub import snapshot_download; from mossy.expressions import LIBRARIES; "
        "[snapshot_download(name, repo_type='dataset', revision=revision) "
        "for name, (revision, _) in LIBRARIES.items()]"
    )
    print("正在准备表情动作素材…", flush=True)
    fetched = subprocess.run(
        [str(python), "-c", fetch], cwd=RUNTIME, env=direct_environment(), capture_output=True
    )
    if fetched.returncode:
        print("没有取到表情动作素材；语音不受影响，联网后再运行一次 mossy install 即可。")
    link, target = Path.home() / ".local/bin/mossy", RUNTIME / "apps_venv/bin/mossy"
    if link.is_symlink() or not link.exists():
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(target)
        print(f"已链接命令：{link} -> {target}")
        if str(link.parent) not in os.environ.get("PATH", "").split(os.pathsep):
            print(f"请把 {link.parent} 加入 PATH。")
    else:
        print(f"{link} 已存在且不是链接，没有改动它；命令也可以直接用 {target}。")
    page = f"http://127.0.0.1:{installed_app()['port']}"
    if request("/state", base=page, optional=True) is not None:
        print("MOSS 还在运行旧代码；运行 mossy restart 换成新版本。")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check the installation and say what to fix."""
    problems = 0

    def report(ok: bool | None, label: str, detail: str = "") -> None:
        """``True`` is fine, ``False`` must be fixed and ``None`` is worth knowing."""
        nonlocal problems
        if ok is False:
            problems += 1
        mark = {True: "✓", False: "✗", None: "!"}[ok]
        print(f"{mark} {label}" + (f"：{detail}" if detail else ""))

    report((RUNTIME / ".venv/bin/python3").exists(), "Reachy Mini Control 运行环境", str(RUNTIME))
    try:
        app = installed_app()
    except (StartupError, subprocess.TimeoutExpired) as exc:
        report(False, "apps_venv 中的 mossy", safe_message(exc))
        app = None
    if app:
        here = Path(__file__).resolve().parent
        package = Path(app["package"]).resolve()
        same = package == here or fingerprint(package) == fingerprint(here)
        note = "" if same else "（与当前代码不同，运行 mossy install 同步）"
        report(True if same else None, "apps_venv 中的 mossy", f"{package}{note}")
        report(
            app["configured"],
            "API key",
            "" if app["configured"] else "mossy config set MOSS_API_KEY=…",
        )

    env = Path.home() / ".config/mossy/.env"
    if env.exists() and env.stat().st_mode & 0o077:
        report(None, "设置文件权限", f"其他用户可读，建议 chmod 600 {env}")
    command = shutil.which("mossy")
    report(True if command else None, "mossy 命令", command or "不在 PATH 中，运行 mossy install")
    logs = list(STATE.glob("reachy-daemon*.log"))
    megabytes = sum(path.stat().st_size for path in logs) / 1e6
    hint = "；mossy logs --clean 可清理" if megabytes >= 50 else ""
    report(
        True if megabytes < 50 else None, "日志", f"{len(logs)} 个文件，{megabytes:.0f} MB{hint}"
    )

    daemon = request("/api/daemon/status", optional=True)
    if daemon is None:
        report(None, "Reachy 后台", "未运行；mossy start 会启动它")
    else:
        detail = safe_message(daemon.get("error") or daemon.get("state"))
        report(daemon.get("state") == "running", "Reachy 后台", detail)
        if CAMERA_SOCKET.exists():
            report(True, "音频通路", "本机直连")
        else:
            report(None, "音频通路", "WebRTC 中转；机器人通电后 mossy stop && mossy start")
    if app:
        state = request("/state", base=f"http://127.0.0.1:{app['port']}", optional=True)
        if state is None:
            report(None, "MOSS", "未运行")
        else:
            report(
                bool(state.get("connected")),
                "MOSS-Live 连接",
                safe_message(state.get("error") or ""),
            )
            gestures = state.get("expressions") or {}
            report(
                True if gestures.get("available") else None,
                "表情动作",
                safe_message(gestures.get("error") or ""),
            )
    print("\n" + ("没有发现问题。" if not problems else f"有 {problems} 项需要处理。"))
    return 1 if problems else 0


def cmd_run(args: argparse.Namespace) -> int:
    """Run the app in this terminal; the Reachy daemon must already be running."""
    from mossy.main import main as run_app

    run_app()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mossy",
        description="Reachy Mini 上的 MOSS-Live 语音对话：启动、停止、查看和配置。",
        epilog="第一次使用：mossy install，然后 mossy config set MOSS_API_KEY=…，再 mossy start。",
    )
    commands = parser.add_subparsers(dest="command", metavar="命令")

    def add(
        name: str,
        function: Callable[[argparse.Namespace], int] | None,
        help: str,
        aliases: tuple[str, ...] = (),
    ) -> argparse.ArgumentParser:
        command = commands.add_parser(name, aliases=list(aliases), help=help, description=help)
        command.set_defaults(run=function)
        return command

    for name in ("start", "restart"):
        command = add(
            name,
            cmd_start,
            "一键启动 Reachy 后台和 MOSS" if name == "start" else "重启 MOSS 以加载新的代码或设置",
        )
        command.add_argument("--no-browser", action="store_true", help="就绪后不打开浏览器")
        if name == "start":
            command.add_argument("--restart", action="store_true", help="等同于 mossy restart")
        else:
            command.set_defaults(restart=True)
    stop = add(
        "stop",
        cmd_stop,
        "停止 MOSS 并关闭由 mossy 启动的 Reachy 后台：机器人休眠，云端会话关闭，媒体设备释放",
        aliases=["end"],
    )
    daemon = stop.add_mutually_exclusive_group()
    daemon.add_argument(
        "--keep-daemon",
        action="store_true",
        help="只停止 MOSS，保留 Reachy 后台：下次启动更快，但后台继续持有机器人的媒体设备",
    )
    daemon.add_argument(
        "--daemon", action="store_true", help="旧用法，现在是默认行为：同时关闭 Reachy 后台"
    )
    stop.epilog = (
        "只会关闭 mossy start 自己启动的 Reachy 后台，并等它的进程退出。"
        "由 Reachy Mini Control 或其他程序启动的后台保持运行，命令会说明它被保留；"
        "正在运行其他应用时不做任何改动。"
    )
    status = add("status", cmd_status, "查看后台、应用、会话和实测延迟")
    status.add_argument("--json", action="store_true", help="输出 JSON")
    add("gui", cmd_gui, "打开控制页面（音色、姿态、API 参数）；未运行时先启动")
    logs = add("logs", cmd_logs, "查看后台和 MOSS 的日志")
    logs.add_argument("-f", "--follow", action="store_true", help="持续输出新日志")
    logs.add_argument("-n", "--lines", type=int, default=60, help="显示最后多少行（默认 60）")
    logs.add_argument("--all", action="store_true", help="包含信令和访问日志")
    logs.add_argument("--clean", action="store_true", help="删除旧日志，只保留最新一个")
    config = add("config", cmd_config, "查看或修改设置；运行中时立即生效")
    config.add_argument(
        "action", nargs="?", default="show", choices=["show", "get", "set", "unset", "path", "edit"]
    )
    config.add_argument("items", nargs="*", metavar="名称[=值]")
    voices = add("voices", cmd_voices, "列出账号下的音色，或切换音色")
    voices.add_argument("--use", metavar="ID或名字", help="切换到这个音色")
    install = add("install", cmd_install, "安装到 Reachy 的 apps_venv，并链接 mossy 命令")
    install.add_argument("-e", "--editable", action="store_true", help="开发模式：直接使用源码目录")
    install.add_argument("--source", help="源码目录（默认是当前这份代码）")
    add("doctor", cmd_doctor, "检查安装和运行环境")
    add("run", cmd_run, "在当前终端前台运行应用（开发用，需要后台已启动）")
    help_command = add("help", None, "显示帮助")
    help_command.add_argument("topic", nargs="?", help="要查看的命令")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "help"):
        if getattr(args, "topic", None):
            parser.parse_args([args.topic, "--help"])  # Prints that command's help and exits.
        parser.print_help()
        return 0
    try:
        return args.run(args)
    except (StartupError, subprocess.TimeoutExpired) as exc:
        prefix = "启动未完成：" if args.run in (cmd_start, cmd_gui) else "未完成："
        print(prefix + safe_message(exc), file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
