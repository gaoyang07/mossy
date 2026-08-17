import os


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("", "0", "false", "no", "off")


LIVE_URL = os.environ.get("MOSS_LIVE_URL", "https://47.116.143.69:7635").rstrip("/")

# The deployment presents a self-signed certificate issued by "MOSS-Live Local CA"
# for a bare IP, so verification is off by default. Turn it on once the server has
# a certificate the robot trusts.
LIVE_VERIFY_TLS = _flag("MOSS_LIVE_VERIFY_TLS", False)

# Empty means "whatever the server defaults to". Query GET {LIVE_URL}/api/personas
# and /api/voices for the values this deployment accepts.
LIVE_PERSONA = os.environ.get("MOSS_LIVE_PERSONA", "")
LIVE_VOICE = os.environ.get("MOSS_LIVE_VOICE", "")

# Drop microphone input while the robot is speaking. Needed whenever the audio
# stack has no working echo cancellation, otherwise MOSS-Live's server-side VAD
# hears the speaker and the robot interrupts itself. Turn it off to get barge-in
# back on hardware whose AEC works (Reachy Mini wireless routes through the XMOS
# loopback declared in .asoundrc).
HALF_DUPLEX = _flag("MOSS_HALF_DUPLEX", True)

WEB_UI_PORT = int(os.environ.get("MOSS_WEB_UI_PORT", "7863"))

AUDIO_GAIN = float(os.environ.get("MOSS_AUDIO_GAIN", "3.0"))
