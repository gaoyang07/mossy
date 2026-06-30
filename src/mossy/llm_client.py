"""LLM client using OpenAI-compatible API (works with Ollama, vLLM, etc.)."""

import logging

import httpx

from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def chat_completion(messages: list[dict[str, str]]) -> str:
    full_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + messages

    try:
        resp = httpx.post(
            f"{LLM_BASE_URL}/chat/completions",
            json={
                "model": LLM_MODEL,
                "messages": full_messages,
                "temperature": 0.7,
                "max_tokens": 256,
            },
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        logger.error("LLM API error: %s %s", e.response.status_code, e.response.text)
        return "抱歉，我暂时无法回答这个问题。"
    except Exception as e:
        logger.error("LLM request failed: %s", e)
        return "抱歉，连接 LLM 服务失败。"
