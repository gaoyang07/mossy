"""MOSS - Main entry point for the Reachy Mini conversational AI app."""

import logging
import shutil
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from reachy_mini.apps.app import ReachyMiniApp
from reachy_mini.reachy_mini import ReachyMini

from . import asr_client, llm_client, tts_client
from .config import WEB_UI_PORT

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
VOICE_DIR = Path(__file__).parent / "voices"


class Moss(ReachyMiniApp):
    """MOSS conversational AI app for Reachy Mini."""

    custom_app_url = f"http://0.0.0.0:{WEB_UI_PORT}"
    dont_start_webserver = True

    def __init__(self) -> None:
        super().__init__()
        self.conversation: list[dict[str, str]] = []
        self.reachy: ReachyMini | None = None
        self._processing = False
        self._ref_audio: Path | None = None
        self._speak_lock = threading.Lock()
        self._stop_speak = threading.Event()
        VOICE_DIR.mkdir(exist_ok=True)

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        self.reachy = reachy_mini

        reachy_mini.enable_motors()
        time.sleep(0.2)
        reachy_mini.wake_up()
        reachy_mini.enable_wobbling()

        server_thread = threading.Thread(target=self._run_server, daemon=True)
        server_thread.start()

        logger.info(f"MOSS is running. Web UI at http://localhost:{WEB_UI_PORT}")

        while not stop_event.is_set():
            stop_event.wait(timeout=1.0)

        reachy_mini.disable_wobbling()
        reachy_mini.goto_sleep()
        reachy_mini.disable_motors()

    def _run_server(self) -> None:
        import uvicorn

        app = FastAPI()

        @app.get("/")
        async def index():
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

        @app.post("/chat")
        async def chat(request: Request):
            body = await request.json()
            user_msg = body.get("message", "").strip()
            if not user_msg:
                return JSONResponse({"error": "empty message"}, status_code=400)

            if self._processing:
                return JSONResponse({"error": "busy"}, status_code=429)

            self._processing = True
            try:
                self.conversation.append({"role": "user", "content": user_msg})
                reply = llm_client.chat_completion(self.conversation)
                self.conversation.append({"role": "assistant", "content": reply})

                threading.Thread(target=self._speak, args=(reply,), daemon=True).start()

                return JSONResponse({"reply": reply})
            finally:
                self._processing = False

        @app.post("/transcribe")
        async def transcribe(file: UploadFile):
            if self._processing:
                return JSONResponse({"error": "busy"}, status_code=429)

            self._processing = True
            try:
                audio_data = await file.read()
                filename = file.filename or "recording.webm"
                text = asr_client.transcribe(audio_data, filename)
                if not text:
                    return JSONResponse({"error": "语音识别失败，请重试"}, status_code=500)

                self.conversation.append({"role": "user", "content": text})
                reply = llm_client.chat_completion(self.conversation)
                self.conversation.append({"role": "assistant", "content": reply})

                threading.Thread(target=self._speak, args=(reply,), daemon=True).start()

                return JSONResponse({"user_text": text, "reply": reply})
            finally:
                self._processing = False

        @app.post("/upload-voice")
        async def upload_voice(file: UploadFile):
            if file.filename is None:
                return JSONResponse({"error": "no file"}, status_code=400)

            suffix = Path(file.filename).suffix.lower()
            if suffix not in (".wav", ".mp3"):
                return JSONResponse({"error": "仅支持 WAV 或 MP3 格式"}, status_code=400)

            dest = VOICE_DIR / f"ref_voice{suffix}"
            with open(dest, "wb") as f:
                shutil.copyfileobj(file.file, f)

            self._ref_audio = dest
            logger.info(f"Voice reference uploaded: {dest} ({dest.stat().st_size} bytes)")
            return JSONResponse(
                {
                    "status": "ok",
                    "filename": file.filename,
                    "size": dest.stat().st_size,
                }
            )

        @app.post("/remove-voice")
        async def remove_voice():
            if self._ref_audio and self._ref_audio.exists():
                self._ref_audio.unlink()
            self._ref_audio = None
            return JSONResponse({"status": "ok"})

        @app.get("/voice-status")
        async def voice_status():
            if self._ref_audio and self._ref_audio.exists():
                return JSONResponse(
                    {
                        "has_voice": True,
                        "filename": self._ref_audio.name,
                        "size": self._ref_audio.stat().st_size,
                    }
                )
            return JSONResponse({"has_voice": False})

        @app.post("/clear")
        async def clear():
            self.conversation.clear()
            return JSONResponse({"status": "ok"})

        uvicorn.run(app, host="0.0.0.0", port=WEB_UI_PORT, log_level="warning")

    def _speak(self, text: str) -> None:
        if self.reachy is None:
            return

        self._stop_speak.set()

        with self._speak_lock:
            self._stop_speak.clear()

            try:
                self.reachy.media.start_playing()

                total_samples = 0
                for chunk in tts_client.synthesize_stream(text, self._ref_audio):
                    if self._stop_speak.is_set():
                        break
                    total_samples += len(chunk)
                    self.reachy.media.push_audio_sample(chunk)

                if total_samples == 0:
                    logger.warning("TTS stream returned no data, skipping")
                    return

                playback_secs = total_samples / 16000.0
                logger.info("TTS pushed %.1fs of audio, waiting for playback", playback_secs)
                self._stop_speak.wait(timeout=playback_secs + 0.5)

            except Exception as e:
                logger.error(f"Streaming playback failed: {e}")
            finally:
                self.reachy.media.stop_playing()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = Moss()
    app.wrapped_run()


if __name__ == "__main__":
    main()
