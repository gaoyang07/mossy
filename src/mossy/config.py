import os


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"{name} must be set in the environment")
    return value


MOSI_TTS_API_KEY = _required_env("MOSI_TTS_API_KEY")
MOSI_TTS_BASE_URL = os.environ.get(
    "MOSI_TTS_BASE_URL",
    "https://api.mosi.cn",
)

MOSI_ASR_API_KEY = _required_env("MOSI_ASR_API_KEY")
MOSI_ASR_BASE_URL = os.environ.get(
    "MOSI_ASR_BASE_URL",
    "https://studio.mosi.cn",
)

LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_API_KEY = _required_env("LLM_API_KEY")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-chat")

SYSTEM_PROMPT = os.environ.get(
    "MOSS_SYSTEM_PROMPT",
    "你是 MOSS，一个运行在 Reachy Mini 机器人上的 AI 助手。"
    "你友善、简洁、有趣，用中文回答问题。"
    "回答控制在两三句话以内，因为你的回复会被转成语音播放。",
)

WEB_UI_PORT = int(os.environ.get("MOSS_WEB_UI_PORT", "7863"))

AUDIO_GAIN = float(os.environ.get("MOSS_AUDIO_GAIN", "3.0"))
