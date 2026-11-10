import os
from pathlib import Path

from dotenv import load_dotenv

# Desktop-launched apps do not inherit a terminal's exported variables. Keep
# credentials outside the checkout; explicit environment variables win.
load_dotenv(Path.home() / ".config" / "mossy" / ".env")
load_dotenv(Path.cwd() / ".env")


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("", "0", "false", "no", "off")


LIVE_URL = os.environ.get("MOSS_LIVE_URL", "https://api.mosi.cn").rstrip("/")
API_KEY = os.environ.get("MOSS_API_KEY", "")
LIVE_MODEL = os.environ.get("MOSS_LIVE_MODEL", "moss-live-1.0")
LIVE_VOICE_ID = os.environ.get("MOSS_LIVE_VOICE_ID", "").strip()
LIVE_USER = os.environ.get("MOSS_LIVE_USER", "reachy-mini")
LIVE_VERIFY_TLS = _flag("MOSS_LIVE_VERIFY_TLS", True)
LIVE_NAME = os.environ.get("MOSS_LIVE_NAME", "MOSS")
LIVE_PROMPT = os.environ.get("MOSS_LIVE_PROMPT", "")
LIVE_INSTRUCTIONS = os.environ.get("MOSS_LIVE_INSTRUCTIONS", "请用简洁自然的中文回答。")

# Drop microphone input while the robot is speaking. Needed whenever the audio
# stack has no working echo cancellation, otherwise MOSS-Live's server-side VAD
# hears the speaker and the robot interrupts itself. Turn it off to get barge-in
# back on hardware whose AEC works (Reachy Mini wireless routes through the XMOS
# loopback declared in .asoundrc).
HALF_DUPLEX = _flag("MOSS_HALF_DUPLEX", True)

WEB_UI_PORT = int(os.environ.get("MOSS_WEB_UI_PORT", "7863"))

AUDIO_GAIN = float(os.environ.get("MOSS_AUDIO_GAIN", "1.0"))

EXPRESSIONS_ENABLED = _flag("MOSS_EXPRESSIONS", True)
EXPRESSION_INTENSITY = float(os.environ.get("MOSS_EXPRESSION_INTENSITY", "0.45"))

CAMERA_ENABLED = _flag("MOSS_CAMERA", False)
CAMERA_FPS = float(os.environ.get("MOSS_CAMERA_FPS", "1"))
CAMERA_MAX_EDGE = int(os.environ.get("MOSS_CAMERA_MAX_EDGE", "640"))
CAMERA_JPEG_QUALITY = int(os.environ.get("MOSS_CAMERA_JPEG_QUALITY", "80"))
