from pathlib import Path
from typing import Protocol


class TTSService(Protocol):
    def synthesize(self, text: str, output_path: Path) -> Path:
        """Synthesize text into an audio artifact and return its path."""


class Pyttsx3TTSService:
    """Local TTS adapter using the Windows SAPI voice installed on the host."""

    def __init__(self) -> None:
        try:
            import pyttsx3
        except ImportError as exc:
            raise RuntimeError("pyttsx3 is not installed") from exc
        self._pyttsx3 = pyttsx3

    def synthesize(self, text: str, output_path: Path) -> Path:
        if not text.strip():
            raise ValueError("Cannot synthesize empty text")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        engine = self._pyttsx3.init()
        try:
            engine.save_to_file(text, str(output_path))
            engine.runAndWait()
        finally:
            engine.stop()
        if not output_path.exists() or output_path.stat().st_size == 0:
            raise RuntimeError("TTS did not produce an audio file")
        return output_path