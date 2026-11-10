#!/bin/bash
# Double-click in Finder, or run: ./start-mossy.command [--restart] [--no-browser]
# Uses the existing Reachy Mini Control installation; contains no credentials.

runtime_python="$HOME/Library/Application Support/com.pollen-robotics.reachy-mini/.venv/bin/python3"
if [ ! -x "$runtime_python" ]; then
    printf '找不到 Reachy Mini Control 的 Python 环境，请先完成它的初始化。\n'
    if [ -t 0 ]; then read -r -p '按回车关闭窗口…' reply; fi
    exit 1
fi

PYTHONIOENCODING=utf-8 "$runtime_python" - "$@" <<'PY'
"""Start the installed macOS robot runtime and managed mossy app."""

import argparse
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

RUNTIME = Path.home() / "Library/Application Support/com.pollen-robotics.reachy-mini"
STATE = Path.home() / ".local/state/mossy"
DAEMON = "http://127.0.0.1:8000"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
STARTUP_TIMEOUT = 180  # A first GStreamer registry scan can take over a minute.


class StartupError(Exception):
    pass


def safe_message(value):
    return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", str(value))[:400]


def request(route, *, base=DAEMON, method="GET", optional=False):
    req = urllib.request.Request(base + route, method=method)
    try:
        with HTTP.open(req, timeout=20 if method == "POST" else 2) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise StartupError(f"接口 {route} 返回 HTTP {exc.code}。") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        if optional:
            return None
        raise StartupError(f"无法连接 {base}，请检查 Reachy 后台。") from exc
    except (ValueError, UnicodeError) as exc:
        raise StartupError(f"{base} 未返回有效的 Reachy/MOSS 状态。") from exc


def direct_environment():
    env = os.environ.copy()
    # This launcher itself runs in Reachy's Python. Its .pth bootstrap has
    # already populated these values. A child Python bootstraps again, even
    # prepending to scalar scanner/registry paths and making them invalid.
    # Follow reachy_mini.apps.manager's child-env isolation; also prevent the
    # config probe in apps_venv from importing the daemon venv's packages.
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


def settings():
    python = RUNTIME / "apps_venv/bin/python"
    if not python.exists():
        raise StartupError("找不到 Reachy 应用环境，请先在 apps_venv 中安装 mossy。")
    # Print only readiness and port, never the key itself.
    probe = (
        "import json; from mossy.config import API_KEY, WEB_UI_PORT; "
        "print(json.dumps({'configured': bool(API_KEY), 'port': WEB_UI_PORT}))"
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
        raise StartupError("无法读取 mossy 配置，请确认它已安装到 Reachy 的 apps_venv。")
    data = json.loads(result.stdout)
    if not data["configured"]:
        raise StartupError("请先在 ~/.config/mossy/.env 中设置 MOSS_API_KEY。")
    if not isinstance(data["port"], int) or not 1 <= data["port"] <= 65535:
        raise StartupError("MOSS_WEB_UI_PORT 不是有效端口。")
    return f"http://127.0.0.1:{data['port']}"


def spawn_daemon():
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


def wait_daemon(process=None):
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
        time.sleep(0.5)
    raise StartupError(f"等待 Reachy 后台超过 {STARTUP_TIMEOUT} 秒，请查看 {STATE} 中最新的日志。")


def run_job(route):
    job = request(route, method="POST")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        result = request("/api/apps/job-status/" + job["job_id"])
        if result["status"] == "done":
            return
        if result["status"] == "error":
            raise StartupError("Reachy 后台恢复失败，请检查机器人电源和 USB 连接。")
        time.sleep(0.5)
    raise StartupError("Reachy 后台恢复超时，请在控制台检查状态。")


def current_app():
    current = request("/api/apps/current-app-status")
    if current and current["info"]["name"] != "mossy":
        name = safe_message(current["info"]["name"])
        raise StartupError(f"当前应用是 {name}，请先在 Reachy 控制台停止它，再启动 MOSS。")
    return current


def wait_connected(base, seconds):
    deadline = time.monotonic() + seconds
    next_notice = time.monotonic() + 15
    while time.monotonic() < deadline:
        data = request("/state", base=base, optional=True)
        if data and data.get("connected"):
            return data
        current = current_app()
        if current and current.get("state") == "error":
            raise StartupError("MOSS 启动失败：" + safe_message(current.get("error", "")))
        daemon = request("/api/daemon/status")
        if daemon.get("state") == "error":
            raise StartupError("机器人连接异常：" + safe_message(daemon.get("error", "")))
        if time.monotonic() >= next_notice:
            print("等待 MOSS 初始化媒体并连接云端…", flush=True)
            next_notice = time.monotonic() + 15
        time.sleep(0.5)
    return None


def start(base, restart=False):
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
        raise StartupError("MOSS 正在停止，请等待几秒后再运行脚本。")
    if current and current["state"] in ("running", "starting") and not restart:
        ready = wait_connected(base, 8)
        if ready:
            print("MOSS 已经在运行，直接使用现有连接。", flush=True)
            return ready
    print("正在启动 MOSS…", flush=True)
    route = "/api/apps/restart-current-app" if current else "/api/apps/start-app/mossy"
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


def main():
    parser = argparse.ArgumentParser(
        prog="start-mossy.command", description="一键启动 Reachy Mini 和 MOSS"
    )
    parser.add_argument("--restart", action="store_true", help="重启 MOSS 以加载修改后的配置")
    parser.add_argument("--no-browser", action="store_true", help="就绪后不打开浏览器")
    args = parser.parse_args()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(STATE / "start.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("已有启动流程正在执行，请稍等。")
            return 0
        base = settings()
        ready = start(base, restart=args.restart)
        print(f"MOSS 已就绪：{base}/", flush=True)
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
        if not args.no_browser:
            subprocess.run(["/usr/bin/open", base + "/"], check=False)
        print("服务在后台运行，可以关闭此终端窗口。", flush=True)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (StartupError, subprocess.TimeoutExpired) as exc:
        print("启动未完成：" + safe_message(exc), file=sys.stderr, flush=True)
        sys.exit(1)
PY
result=$?
if [ "$result" -ne 0 ] && [ -t 0 ]; then
    read -r -p '按回车关闭窗口…' reply
fi
exit "$result"
