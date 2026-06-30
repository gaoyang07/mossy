"""ASR client for studio.mosi.cn MOSS-Transcribe async API."""

import logging
import time

import httpx

from .config import MOSI_ASR_API_KEY, MOSI_ASR_BASE_URL

logger = logging.getLogger(__name__)

_HEADERS = {"Authorization": f"Bearer {MOSI_ASR_API_KEY}"}
_UPLOAD_URL = f"{MOSI_ASR_BASE_URL}/api/v1/files/upload"
_TASKS_URL = f"{MOSI_ASR_BASE_URL}/api/v1/asr/tasks"


def transcribe(audio_data: bytes, filename: str) -> str | None:
    """Upload audio, create async ASR task, poll until done.

    Returns transcribed text on success, None on failure.
    """
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                _UPLOAD_URL,
                headers=_HEADERS,
                files={"file": (filename, audio_data)},
            )
            resp.raise_for_status()
            file_id = resp.json()["file_id"]
            logger.info("ASR file uploaded: file_id=%s", file_id)

            resp = client.post(
                _TASKS_URL,
                headers={**_HEADERS, "Content-Type": "application/json"},
                json={"file_id": file_id, "model": "moss-transcribe"},
            )
            resp.raise_for_status()
            task_id = resp.json()["task_id"]
            logger.info("ASR task created: task_id=%s", task_id)

            for _ in range(60):
                time.sleep(1.0)
                resp = client.get(
                    f"{_TASKS_URL}/{task_id}",
                    headers=_HEADERS,
                )
                resp.raise_for_status()
                data = resp.json()
                status = data.get("status")

                if status == "SUCCESS":
                    text = data.get("result_text", "").strip()
                    logger.info("ASR completed: %s", text[:100])
                    return text
                if status == "FAILED":
                    logger.error("ASR task failed: %s", data.get("error"))
                    return None

            logger.error("ASR task timed out after 60s")
            return None

    except httpx.HTTPStatusError as e:
        logger.error("ASR API error: %s %s", e.response.status_code, e.response.text)
        return None
    except Exception as e:
        logger.error("ASR failed: %s", e)
        return None
