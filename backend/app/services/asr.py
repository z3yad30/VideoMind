from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backend.app.core.config import settings


@dataclass(frozen=True)
class ASRSegment:
    text: str
    start: float
    end: float


class ASRService(Protocol):
    def transcribe(self, audio_path: Path) -> list[ASRSegment]:
        """Transcribe audio and preserve source timestamps in seconds."""


class FasterWhisperASRService:
    def __init__(self, model_name: str = settings.asr_model, device: str = settings.asr_device, compute_type: str = settings.asr_compute_type) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper is not installed") from exc
        self._model = WhisperModel(model_name, device=device, compute_type=compute_type)

    def transcribe(self, audio_path: Path) -> list[ASRSegment]:
        segments, _ = self._model.transcribe(str(audio_path))
        return [ASRSegment(segment.text, float(segment.start), float(segment.end)) for segment in segments if segment.text and segment.text.strip()]