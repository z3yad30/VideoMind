from pathlib import Path
from typing import Any

from backend.app.ports import TTS


class Pyttsx3TTS:
    def __init__(self, engine_module: Any | None = None) -> None:
        self._pyttsx3 = engine_module

    def synthesize(self, text: str, out_path: Path) -> Path:
        if not text.strip():
            raise ValueError("Cannot synthesize empty text")
        if self._pyttsx3 is None:
            try:
                import pyttsx3
            except ImportError as exc:
                raise RuntimeError("pyttsx3 is not installed") from exc
            self._pyttsx3 = pyttsx3
        out_path.parent.mkdir(parents=True, exist_ok=True)
        engine = self._pyttsx3.init()
        try:
            engine.save_to_file(text, str(out_path))
            engine.runAndWait()
        finally:
            engine.stop()
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("TTS did not produce an audio file")
        return out_path
