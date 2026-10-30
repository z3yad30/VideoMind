from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Segment:
    text: str
    start: float
    end: float


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> list[Segment]: ...


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorStore(Protocol):
    def upsert(self, video_id: str, chunks: list[dict[str, object]]) -> None: ...

    def query(self, video_id: str, embedding: list[float], top_k: int) -> list[dict[str, object]]: ...

    def delete_video(self, video_id: str) -> None: ...


class LLMClient(Protocol):
    def complete(self, messages: list[dict[str, str]], **opts: object) -> str: ...


class TTS(Protocol):
    def synthesize(self, text: str, out_path: Path) -> Path: ...


class BlobStore(Protocol):
    def put(self, key: str, source: Path | bytes | str) -> Path: ...

    def open(self, key: str) -> Path: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...
