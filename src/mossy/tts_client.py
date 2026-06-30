"""TTS client for api.mosi.cn MOSS-TTS API with voice cloning support."""

import json
import logging
import tempfile
from collections.abc import Generator
from pathlib import Path

import httpx
import numpy as np
import numpy.typing as npt
from scipy.signal import resample_poly

from .config import AUDIO_GAIN, MOSI_TTS_API_KEY, MOSI_TTS_BASE_URL

logger = logging.getLogger(__name__)

ENDPOINT = f"{MOSI_TTS_BASE_URL}/v1/audio/speech"

# Mosi TTS PCM output format (from response headers / docs)
MOSI_SAMPLE_RATE = 48000
# Reachy Mini AudioBase.SAMPLE_RATE
REACHY_SAMPLE_RATE = 16000


def synthesize_stream(
    text: str, ref_audio_path: Path | None = None
) -> Generator[npt.NDArray[np.float32], None, None]:
    """Stream TTS as float32 PCM chunks resampled to 16 kHz for push_audio_sample().

    Yields numpy float32 arrays (mono, 16000 Hz).
    """
    request_body = {
        "model": "MOSS-TTS",
        "input": text,
        "stream": True,
        "response_format": "pcm",
    }
    headers = {
        "Authorization": f"Bearer {MOSI_TTS_API_KEY}",
        "Accept": "audio/pcm",
    }

    try:
        with httpx.Client(timeout=60.0) as client:
            if ref_audio_path and ref_audio_path.exists():
                resp = _post_multipart(client, headers, request_body, ref_audio_path)
            else:
                headers["Content-Type"] = "application/json"
                resp = client.send(
                    client.build_request("POST", ENDPOINT, headers=headers, json=request_body),
                    stream=True,
                )

            resp.raise_for_status()

            # Read PCM format from response headers (docs: x-sample-rate, x-channels, x-bit-depth)
            src_rate = int(resp.headers.get("x-sample-rate", MOSI_SAMPLE_RATE))
            src_channels = int(resp.headers.get("x-channels", 1))
            src_bits = int(resp.headers.get("x-bit-depth", 16))
            bytes_per_sample = (src_bits // 8) * src_channels
            dtype = np.int16 if src_bits == 16 else np.int32

            down = src_rate // REACHY_SAMPLE_RATE if src_rate > REACHY_SAMPLE_RATE else 1
            logger.info(
                "TTS stream: %d Hz %dch %dbit → %d Hz (down=%d, clone=%s)",
                src_rate,
                src_channels,
                src_bits,
                REACHY_SAMPLE_RATE,
                down,
                ref_audio_path is not None,
            )

            leftover = b""
            for chunk in resp.iter_bytes(chunk_size=8192):
                if not chunk:
                    continue
                raw = leftover + chunk
                usable = len(raw) - (len(raw) % bytes_per_sample)
                leftover = raw[usable:]

                samples = np.frombuffer(raw[:usable], dtype=dtype).astype(np.float32) / (
                    2 ** (src_bits - 1)
                )
                # If stereo, take first channel
                if src_channels > 1:
                    samples = samples[::src_channels]

                if len(samples) < down:
                    continue

                if down > 1:
                    samples = resample_poly(samples, up=1, down=down).astype(np.float32)

                if AUDIO_GAIN != 1.0:
                    samples = np.clip(samples * AUDIO_GAIN, -1.0, 1.0).astype(np.float32)

                yield samples

            resp.close()
            logger.info("TTS stream finished")

    except httpx.HTTPStatusError as e:
        try:
            e.response.read()
            err_body = e.response.text
        except Exception:
            err_body = "<unreadable>"
        logger.error("TTS API error: %s %s", e.response.status_code, err_body)
    except Exception as e:
        logger.error("TTS stream failed: %s", e)


def synthesize(text: str, ref_audio_path: Path | None = None) -> Path | None:
    """Non-streaming: returns a complete WAV file path."""
    request_body = {
        "model": "MOSS-TTS",
        "input": text,
        "stream": False,
        "response_format": "wav",
    }
    headers = {
        "Authorization": f"Bearer {MOSI_TTS_API_KEY}",
        "Accept": "audio/wav",
    }

    try:
        if ref_audio_path and ref_audio_path.exists():
            with httpx.Client(timeout=30.0) as client:
                resp = _post_multipart(client, headers, request_body, ref_audio_path)
                resp.read()
        else:
            headers["Content-Type"] = "application/json"
            resp = httpx.post(ENDPOINT, headers=headers, json=request_body, timeout=30.0)

        resp.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.write(resp.content)
        tmp.close()
        logger.info("TTS synthesized %d bytes", len(resp.content))
        return Path(tmp.name)

    except httpx.HTTPStatusError as e:
        logger.error("TTS API error: %s %s", e.response.status_code, e.response.text)
        return None
    except Exception as e:
        logger.error("TTS request failed: %s", e)
        return None


def _post_multipart(
    client: httpx.Client,
    headers: dict,
    request_body: dict,
    ref_audio_path: Path,
) -> httpx.Response:
    """POST with multipart form: request_json + ref_audio."""
    with open(ref_audio_path, "rb") as f:
        return client.send(
            client.build_request(
                "POST",
                ENDPOINT,
                headers={k: v for k, v in headers.items() if k.lower() != "content-type"},
                data={"request_json": json.dumps(request_body)},
                files={"ref_audio": (ref_audio_path.name, f, _guess_mime(ref_audio_path))},
            ),
            stream=request_body.get("stream", False),
        )


def _guess_mime(path: Path) -> str:
    return {".wav": "audio/wav", ".mp3": "audio/mpeg"}.get(
        path.suffix.lower(), "application/octet-stream"
    )
