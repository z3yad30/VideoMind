import json
from pathlib import Path
from typing import Any, Protocol

from backend.app.core.config import settings
from backend.app.services.rag import TranscriptRAGService


SUMMARY_FIELDS = (
    "Overview",
    "Main Topics",
    "Key Concepts",
    "Important Details",
    "Key Takeaways",
)


class ChatClient(Protocol):
    def create(self, *, messages: list[dict[str, str]], model: str, temperature: float) -> Any:
        """Create a chat completion."""


class GroqLLMService:
    def __init__(self, api_key: str | None = None, model: str | None = None, client: ChatClient | None = None) -> None:
        self.api_key = api_key if api_key is not None else settings.groq_api_key
        self.model = model or settings.groq_model or "openai/gpt-oss-120b"
        self._client = client

    @property
    def client(self) -> ChatClient:
        if self._client is None:
            if not self.api_key:
                raise RuntimeError("GROQ_API_KEY is not configured")
            try:
                from groq import Groq
            except ImportError as exc:
                raise RuntimeError("groq is not installed") from exc
            self._client = Groq(api_key=self.api_key).chat.completions
        return self._client

    def complete(self, prompt: str) -> str:
        response = self.client.create(
            model=self.model,
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content.strip()

    def summarize(self, text: str) -> dict[str, str]:
        prompt = f"""Summarize this video transcript using exactly these headings: {', '.join(SUMMARY_FIELDS)}.
Return valid JSON with those exact keys and string values. Do not add other keys.
Use only information in the transcript. Do not hallucinate or infer unsupported facts.

TRANSCRIPT:
{text}"""
        return self._parse_summary(self.complete(prompt))

    def summarize_hierarchically(self, texts: list[str], max_prompt_characters: int = 12000) -> dict[str, str]:
        if not texts:
            return {field: "" for field in SUMMARY_FIELDS}
        batches: list[str] = []
        current: list[str] = []
        current_size = 0
        for text in texts:
            if current and current_size + len(text) > max_prompt_characters:
                batches.append("\n\n".join(current))
                current = []
                current_size = 0
            current.append(text)
            current_size += len(text)
        if current:
            batches.append("\n\n".join(current))

        partials = [self.summarize(batch) for batch in batches]
        while len(partials) > 1:
            partial_text = "\n\n".join(json.dumps(item) for item in partials)
            partials = [self.summarize(partial_text)]
        return partials[0]

    def answer(self, question: str, chunks: list[dict[str, object]]) -> str:
        context = "\n\n".join(
            f"[{item['metadata']['start']}-{item['metadata']['end']}] {item['text']}"
            for item in chunks
        )
        prompt = f"""Answer the user's question using only the retrieved video transcript context below.
Do not hallucinate, guess, or use outside knowledge. If the context does not contain enough information,
answer exactly: The answer cannot be determined from the video context.
Include no citations or sources in your answer; sources are returned separately by the application.

USER QUESTION:
{question}

VIDEO CONTEXT:
{context or '[No relevant video context was retrieved.]'}"""
        return self.complete(prompt)

    @staticmethod
    def _parse_summary(content: str) -> dict[str, str]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = {}
        return {field: str(parsed.get(field, "")) for field in SUMMARY_FIELDS}


class VideoAIService:
    def __init__(
        self,
        llm: GroqLLMService | None = None,
        rag: TranscriptRAGService | None = None,
        summary_dir: Path | None = None,
    ) -> None:
        self.llm = llm or GroqLLMService()
        self.rag = rag
        self.summary_dir = summary_dir or settings.project_root / "data" / "summaries"

    def _rag(self) -> TranscriptRAGService:
        if self.rag is None:
            self.rag = TranscriptRAGService()
        return self.rag

    def index_and_summarize(self, video_id: str) -> None:
        chunks = self._rag().index_transcript(video_id)
        summary = self.llm.summarize_hierarchically([str(chunk["text"]) for chunk in chunks])
        self.summary_dir.mkdir(parents=True, exist_ok=True)
        (self.summary_dir / f"{video_id}.json").write_text(
            json.dumps({"video_id": video_id, "summary": summary}, indent=2), encoding="utf-8"
        )

    def get_summary(self, video_id: str) -> dict[str, str] | None:
        path = self.summary_dir / f"{video_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))["summary"]
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def answer_question(self, video_id: str, question: str, top_k: int = 5) -> dict[str, object]:
        chunks = self._rag().retrieve_relevant_chunks(video_id, question, top_k)
        answer = self.llm.answer(question, chunks)
        sources = [
            {"start": float(item["metadata"]["start"]), "end": float(item["metadata"]["end"]), "text": str(item["text"])}
            for item in chunks
            if isinstance(item.get("metadata"), dict)
            and "start" in item["metadata"]
            and "end" in item["metadata"]
        ]
        return {"answer": answer, "sources": sources}