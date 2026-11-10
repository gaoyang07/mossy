#!/bin/bash
# Double-click in Finder, or run: ./start-mossy.command [--restart] [--no-browser]
# The same as `mossy start`, for a checkout whose `mossy` command is not on PATH.
# Uses the existing Reachy Mini Control installation; contains no credentials.

runtime_python="$HOME/Library/Application Support/com.pollen-robotics.reachy-mini/.venv/bin/python3"
if [ ! -x "$runtime_python" ]; then
    printf '找不到 Reachy Mini Control 的 Python 环境，请先完成它的初始化。\n'
    if [ -t 0 ]; then read -r -p '按回车关闭窗口…' reply; fi
    exit 1
fi

# The command needs only the standard library, so any of Reachy's Pythons can
# run it straight from this checkout.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHONIOENCODING=utf-8 PYTHONPATH="$here/src" "$runtime_python" -m mossy.cli start "$@"
result=$?
if [ "$result" -ne 0 ] && [ -t 0 ]; then
    read -r -p '按回车关闭窗口…' reply
fi
exit "$result"
