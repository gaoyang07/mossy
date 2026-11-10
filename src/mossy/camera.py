"""Read Reachy SDK frames and prepare bounded JPEGs off the audio thread."""

import io
import logging
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)
MAX_FRAME_AGE = 3.0
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def local_camera_access(
    client_host: str | None, host: str, origin: str | None, fetch_site: str | None
) -> bool:
    """Keep preview/control on this machine and reject cross-site browser calls."""
    if client_host not in {"127.0.0.1", "::1"}:
        return False
    try:
        if urlsplit("//" + host).hostname not in LOOPBACK_HOSTS:
            return False
        if origin and urlsplit(origin).netloc != host:
            return False
    except ValueError:
        return False
    return fetch_site in (None, "none", "same-origin")


def encode_frame(frame: np.ndarray, max_edge: int, quality: int) -> bytes:
    """Convert the SDK's uint8 BGR frame to a JPEG, preserving aspect ratio."""
    if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("Expected a uint8 BGR frame from Reachy Mini")
    if min(frame.shape[:2]) == 0:
        raise ValueError("Reachy Mini returned an empty camera frame")
    image = Image.fromarray(frame[:, :, ::-1])
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality)
    return output.getvalue()


class Camera:
    """One camera worker, using only the robot SDK's existing media source.

    The latest JPEG stays in memory for local preview. No frames are written to
    disk. Disabling visual input clears it and stops submissions to MOSS-Live.
    """

    def __init__(
        self,
        get_frame: Callable[[], np.ndarray | None],
        submit_frame: Callable[[bytes, float], None],
        set_source_enabled: Callable[[bool], None],
        *,
        enabled: bool = False,
        fps: float = 1.0,
        max_edge: int = 640,
        quality: int = 80,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0.5 <= fps <= 5.0:
            raise ValueError("MOSS_CAMERA_FPS must be between 0.5 and 5")
        if not 128 <= max_edge <= 1920:
            raise ValueError("MOSS_CAMERA_MAX_EDGE must be between 128 and 1920")
        if not 1 <= quality <= 95:
            raise ValueError("MOSS_CAMERA_JPEG_QUALITY must be between 1 and 95")
        self._get_frame = get_frame
        self._submit_frame = submit_frame
        self._set_source_enabled = set_source_enabled
        self._enabled = enabled
        self._fps, self._max_edge, self._quality = fps, max_edge, quality
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._jpeg: bytes | None = None
        self._captured_at: float | None = None
        self._captured_frames = 0
        self._error: str | None = None

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled
            self._generation += 1
            self._jpeg = None
            self._captured_at = None
            self._error = None
        self._set_source_enabled(enabled)
        self._wake.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            age = self._clock() - self._captured_at if self._captured_at is not None else None
            available = self._jpeg is not None and age is not None and age <= MAX_FRAME_AGE
            return {
                "enabled": self._enabled,
                "available": available,
                "fps": self._fps,
                "max_edge": self._max_edge,
                "captured_frames": self._captured_frames,
                "last_frame_age_s": round(age, 2) if age is not None else None,
                "error": self._error,
            }

    def preview(self) -> bytes | None:
        with self._lock:
            if (
                self._enabled
                and self._captured_at is not None
                and self._clock() - self._captured_at <= MAX_FRAME_AGE
            ):
                return self._jpeg
        return None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="moss-camera", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.set_enabled(False)
        if self._thread is not None:
            self._thread.join()

    def _capture_once(self) -> None:
        with self._lock:
            if not self._enabled or self._stop.is_set():
                return
            generation = self._generation
        try:
            frame = self._get_frame()
            if frame is None:
                raise RuntimeError(
                    "No Reachy camera frames; check the daemon and camera permission"
                )
            captured_at = self._clock()
            jpeg = encode_frame(frame, self._max_edge, self._quality)
            with self._lock:
                if generation != self._generation or not self._enabled or self._stop.is_set():
                    return
                self._jpeg = jpeg
                self._captured_at = captured_at
                self._captured_frames += 1
                self._error = None
            self._submit_frame(jpeg, captured_at)
        except Exception as exc:
            with self._lock:
                if generation != self._generation:
                    return
                previous = self._error
                self._error = str(exc)
                self._jpeg = None
            if previous != str(exc):
                logger.warning("Camera input unavailable; voice continues: %s", exc)

    def _run(self) -> None:
        interval = 1.0 / self._fps
        while not self._stop.is_set():
            started_at = self._clock()
            self._capture_once()
            self._wake.wait(max(0.0, interval - (self._clock() - started_at)))
            self._wake.clear()
