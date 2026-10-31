import logging
import threading
from pathlib import Path
from typing import Any

from backend.app.core.config import settings
from backend.app.ports import Segment, Transcriber

logger = logging.getLogger(__name__)


class FasterWhisperTranscriber:
    def __init__(
        self,
        model_name: str = settings.asr_model,
        device: str = settings.asr_device,
        compute_type: str = settings.asr_compute_type,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.compute_type = compute_type
        self._model: Any | None = None
        self._lock = threading.Lock()

    def _new_model(self, device: str, compute_type: str) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError("faster-whisper is not installed") from exc
        return WhisperModel(
            self.model_name,
            device=device,
            compute_type=compute_type,
        )

    def _get_model(self) -> Any:
        if self._model is None:
            with self._lock:
                if self._model is None:
                    self._model = self._new_model(self.device, self.compute_type)
        return self._model

    @staticmethod
    def _is_missing_cuda_library(exc: RuntimeError) -> bool:
        message = str(exc).casefold()
        return (
            ("cublas" in message or "cudnn" in message)
            and ("not found" in message or "cannot be loaded" in message or "failed to load" in message)
        )

    def transcribe(self, audio_path: Path) -> list[Segment]:
        logger.info("Starting transcription for %s", audio_path.name)
        try:
            segments, _ = self._get_model().transcribe(str(audio_path))
            transcript = [
                Segment(segment.text, float(segment.start), float(segment.end))
                for segment in segments
                if segment.text and segment.text.strip()
            ]
        except RuntimeError as exc:
            if self.device.casefold() != "cuda" or not self._is_missing_cuda_library(exc):
                raise
            logger.warning(
                "CUDA transcription libraries are unavailable; retrying with CPU int8"
            )
            with self._lock:
                self.device = "cpu"
                self.compute_type = "int8"
                self._model = self._new_model(self.device, self.compute_type)
            segments, _ = self._model.transcribe(str(audio_path))
            transcript = [
                Segment(segment.text, float(segment.start), float(segment.end))
                for segment in segments
                if segment.text and segment.text.strip()
            ]
        logger.info("Transcription finished: %s segments", len(transcript))
        return transcript
