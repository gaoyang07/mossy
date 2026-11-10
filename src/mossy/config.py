"""Settings shared by the app, the ``mossy`` CLI and the settings page.

One schema describes every option: its environment variable, type and bounds,
and how a change takes effect. ``live`` applies to the running conversation,
``reconnect`` needs a new MOSS-Live connection and ``restart`` applies the next
time the app starts. Settings are keyed by their environment variable so the
README, the CLI, the page and the code all use the same name.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv, set_key, unset_key

USER_ENV = Path.home() / ".config" / "mossy" / ".env"

# Variables exported before the files below are read. They win over the files,
# so the settings page cannot change them.
_EXPORTED = frozenset(os.environ)

# Desktop-launched apps do not inherit a terminal's exported variables. Keep
# credentials outside the checkout; explicit environment variables win.
load_dotenv(USER_ENV)
load_dotenv(Path.cwd() / ".env")

# Keys of mossy.expressions.POSTURES, repeated here so the CLI can validate
# them without importing NumPy and SciPy.
POSTURES = ("neutral", "attentive", "relaxed", "curious")

GROUPS = {
    "voice": "音色与人设",
    "conversation": "对话",
    "robot": "机器人",
    "vision": "视觉",
    "connection": "连接",
}


@dataclass(frozen=True)
class Field:
    kind: str  # "str", "text", "secret", "bool", "int" or "float"
    default: Any
    group: str
    label: str
    scope: str = "live"  # "live", "reconnect" or "restart"
    help: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple[Any, ...] = ()
    max_length: int | None = None


FIELDS: dict[str, Field] = {
    "MOSS_LIVE_VOICE_ID": Field(
        "str", "", "voice", "音色", help="平台音色 ID；留空使用服务端默认音色"
    ),
    "MOSS_LIVE_SPEED": Field(
        "float", 1.0, "voice", "语速", minimum=0.5, maximum=1.5, step=0.05, help="1 为正常语速"
    ),
    "MOSS_LIVE_VOLUME_DB": Field(
        "float",
        0.0,
        "voice",
        "合成音量 (dB)",
        minimum=-20,
        maximum=16,
        step=1,
        help="服务端合成增益；设备音量请在 Reachy 控制台调节",
    ),
    "MOSS_LIVE_NAME": Field("str", "MOSS", "voice", "助手名字", help="用于识别用户是否在叫它"),
    "MOSS_LIVE_PROMPT": Field(
        "text", "", "voice", "人设提示词", max_length=8000, help="留空使用服务端默认人设"
    ),
    "MOSS_LIVE_INSTRUCTIONS": Field(
        "text", "请用简洁自然的中文回答。", "voice", "会话指令", max_length=16000
    ),
    "MOSS_TIME_ZONE": Field(
        "str",
        "auto",
        "voice",
        "时区",
        help="IANA 时区名，用于回答时间问题；auto 使用本机时区，留空则不发送",
    ),
    "MOSS_HALF_DUPLEX": Field(
        "bool",
        True,
        "conversation",
        "半双工",
        help="机器人说话时屏蔽麦克风；关闭后可随时打断，需要回声消除正常工作",
    ),
    "MOSS_AUDIO_GAIN": Field(
        "float", 1.0, "conversation", "本地播放增益", minimum=0, maximum=4, step=0.1
    ),
    "MOSS_IDLE_SHUTDOWN_MIN": Field(
        "float",
        0.0,
        "conversation",
        "空闲自动停止 (分钟)",
        minimum=0,
        maximum=1440,
        step=1,
        help="这么久没有人说话就让机器人休眠并断开云端会话；0 表示一直在线",
    ),
    "MOSS_POSTURE": Field("str", "neutral", "robot", "待机姿态", choices=POSTURES),
    "MOSS_EXPRESSIONS": Field("bool", True, "robot", "表情动作"),
    "MOSS_EXPRESSION_INTENSITY": Field(
        "float", 0.45, "robot", "动作幅度", minimum=0, maximum=1, step=0.05
    ),
    "MOSS_WAKE_ANIMATION": Field(
        "bool",
        True,
        "robot",
        "唤醒动画",
        scope="restart",
        help="关闭后跳过唤醒音效和摆头，启动约快 1.5 秒",
    ),
    "MOSS_CAMERA": Field("bool", False, "vision", "视觉输入"),
    "MOSS_CAMERA_FPS": Field(
        "float", 1.0, "vision", "帧率 (FPS)", minimum=0.5, maximum=5, step=0.5
    ),
    "MOSS_CAMERA_MAX_EDGE": Field(
        "int", 640, "vision", "最长边 (像素)", minimum=128, maximum=1920, step=64
    ),
    "MOSS_CAMERA_JPEG_QUALITY": Field(
        "int", 80, "vision", "JPEG 质量", minimum=1, maximum=95, step=1
    ),
    "MOSS_API_KEY": Field("secret", "", "connection", "API Key", scope="reconnect"),
    "MOSS_LIVE_URL": Field(
        "str", "https://api.mosi.cn", "connection", "API 地址", scope="reconnect"
    ),
    "MOSS_LIVE_MODEL": Field("str", "moss-live-1.0", "connection", "模型", scope="reconnect"),
    "MOSS_LIVE_USER": Field(
        "str", "reachy-mini", "connection", "用户 ID", scope="reconnect", help="重连时保持不变"
    ),
    "MOSS_LIVE_VERIFY_TLS": Field("bool", True, "connection", "校验 TLS 证书", scope="reconnect"),
    "MOSS_LIVE_OUTPUT_RATE": Field(
        "int",
        16000,
        "connection",
        "下行采样率 (Hz)",
        scope="reconnect",
        choices=(16000, 48000),
        help="16000 与机器人播放采样率一致，下行流量约为 48000 的三分之一",
    ),
    "MOSS_WEB_UI_PORT": Field(
        "int", 7863, "connection", "页面端口", scope="restart", minimum=1, maximum=65535
    ),
}


def coerce(key: str, value: Any) -> Any:
    """Convert text from the environment, or JSON from the page, to a valid value."""
    field = FIELDS[key]
    if field.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() not in ("", "0", "false", "no", "off")
        return bool(value)
    if field.kind in ("int", "float"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a number") from None
        if field.kind == "int":
            if not number.is_integer():
                raise ValueError(f"{key} must be a whole number")
            number = int(number)
        if field.choices and number not in field.choices:
            raise ValueError(f"{key} must be one of {', '.join(map(str, field.choices))}")
        # Written so that NaN fails too.
        low, high = field.minimum, field.maximum
        if (low is not None and not number >= low) or (high is not None and not number <= high):
            raise ValueError(f"{key} must be between {low:g} and {high:g}")
        return number
    text = str(value) if field.kind == "text" else str(value).strip()
    if field.choices and text not in field.choices:
        raise ValueError(f"{key} must be one of {', '.join(field.choices)}")
    if field.max_length is not None and len(text) > field.max_length:
        raise ValueError(f"{key} must be at most {field.max_length} characters")
    return text


def load(environ: Any = os.environ) -> dict[str, Any]:
    """Read every setting, falling back to its default."""
    return {
        key: coerce(key, environ[key]) if key in environ else field.default
        for key, field in FIELDS.items()
    }


def pinned(key: str) -> bool:
    """Whether an exported environment variable overrides the settings file."""
    return key in _EXPORTED


def as_text(key: str, value: Any) -> str:
    if FIELDS[key].kind == "bool":
        return "1" if value else "0"
    return f"{value:g}" if isinstance(value, float) else str(value)


def save(updates: dict[str, Any], path: Path | None = None) -> None:
    """Write values to the user's settings file, leaving its other lines alone.

    ``None`` removes a key so that its default applies again.
    """
    path = path or USER_ENV
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for key, value in updates.items():
        if value is None:
            if path.exists():
                unset_key(path, key)
            continue
        text = as_text(key, value)
        # Double quotes with these escapes round-trip multi-line prompts.
        quoted = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        set_key(path, key, f'"{quoted}"', quote_mode="never")
    if path.exists():
        os.chmod(path, 0o600)  # The file holds the API key.


def describe(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """The schema and current values for the CLI and the page; never the secret."""
    described = []
    for key, field in FIELDS.items():
        secret = field.kind == "secret"
        described.append(
            {
                "key": key,
                "kind": field.kind,
                "group": field.group,
                "label": field.label,
                "help": field.help,
                "scope": field.scope,
                "default": "" if secret else field.default,
                "value": "" if secret else settings[key],
                "is_set": bool(settings[key]),
                "pinned": pinned(key),
                "minimum": field.minimum,
                "maximum": field.maximum,
                "step": field.step,
                "choices": list(field.choices),
                "max_length": field.max_length,
            }
        )
    return described


def local_time_zone() -> str:
    """The machine's IANA time zone name, or an empty string if it is unknown."""
    zone = os.environ.get("TZ", "").lstrip(":")
    if "/" not in zone:
        # /etc/localtime links into the zoneinfo database on macOS and Linux.
        _, found, zone = os.path.realpath("/etc/localtime").partition("/zoneinfo/")
        if not found:
            return ""
    return zone
