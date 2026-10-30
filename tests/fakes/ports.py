import json
from pathlib import Path
from typing import Any

from backend.app.ports import Segment


class FakeBlobStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def open(self, key: str) -> Path:
        path = self.data_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def put(self, key: str, source: Path | bytes | str) -> Path:
        path = self.open(key)
        if isinstance(source, Path):
            path.write_bytes(source.read_bytes())
        elif isinstance(source, bytes):
            path.write_bytes(source)
        else:
            path.write_text(source, encoding="utf-8")
        return path

    def exists(self, key: str) -> bool:
        return (self.data_dir / key).is_file()

    def delete(self, key: str) -> None:
        (self.data_dir / key).unlink(missing_ok=True)

    def write_transcript(self, video_id: str, segments: list[dict[str, object]]) -> None:
        self.put(f"transcripts/{video_id}.json", json.dumps({"video_id": video_id, "segments": segments}))


class FakeTranscriber:
    def __init__(self, segments: list[Segment] | None = None) -> None:
        self.segments = segments or [Segment("first sentence", 0.25, 1.75), Segment("second sentence", 2.0, 3.5)]

    def transcribe(self, audio_path: Path) -> list[Segment]:
        return self.segments


class FakeEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), float(text.lower().count("python"))] for text in texts]


class FakeVectorStore:
    def __init__(self, results: list[dict[str, object]] | None = None) -> None:
        self.results = results or []
        self.chunks: dict[str, list[dict[str, object]]] = {}
        self.queries: list[tuple[str, list[float], int]] = []

    def upsert(self, video_id: str, chunks: list[dict[str, object]]) -> None:
        self.chunks[video_id] = chunks

    def query(self, video_id: str, embedding: list[float], top_k: int) -> list[dict[str, object]]:
        self.queries.append((video_id, embedding, top_k))
        return [chunk for chunk in self.results if chunk.get("metadata", {}).get("video_id", video_id) == video_id][:top_k]

    def delete_video(self, video_id: str) -> None:
        self.chunks.pop(video_id, None)


class FakeLLMClient:
    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses = list(responses or [])
        self.prompts: list[str] = []
        self.options: list[dict[str, object]] = []

    def complete(self, messages: list[dict[str, str]], **opts: object) -> str:
        self.prompts.append(messages[0]["content"])
        self.options.append(opts)
        return self.responses.pop(0)


class FakeTTS:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def synthesize(self, text: str, out_path: Path) -> Path:
        self.calls.append((text, out_path))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"wav")
        return out_path
