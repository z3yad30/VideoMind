import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.services.llm import GroqLLMService, VideoAIService


class FakeCompletions:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.prompts: list[str] = []

    def create(self, *, messages, model, temperature):
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.responses.pop(0)))]
        )


class FakeRAG:
    def __init__(self, chunks):
        self.chunks = chunks

    def retrieve_relevant_chunks(self, video_id, question, top_k):
        return self.chunks


def test_groq_default_model_and_grounded_question_prompt() -> None:
    client = FakeCompletions(["The answer cannot be determined from the video context."])
    service = GroqLLMService(api_key="test-key", client=client)

    assert service.answer("What happened?", []) == "The answer cannot be determined from the video context."
    assert service.model == "openai/gpt-oss-120b"
    assert client.prompts == []


def test_hierarchical_summary_uses_required_structure() -> None:
    summary = {field: field for field in ("Overview", "Main Topics", "Key Concepts", "Important Details", "Key Takeaways")}
    client = FakeCompletions([json.dumps(summary), json.dumps(summary), json.dumps(summary)])
    service = GroqLLMService(api_key="test-key", client=client)

    result = service.summarize_hierarchically(["first transcript", "second transcript"], max_prompt_characters=10)

    assert result == summary
    assert len(client.prompts) == 3


def test_question_sources_are_only_retrieved_chunks(tmp_path: Path) -> None:
    client = FakeCompletions(["Grounded answer"])
    rag = FakeRAG([
        {"text": "A real transcript fact", "metadata": {"start": 120.5, "end": 145.8}},
    ])
    service = VideoAIService(llm=GroqLLMService(api_key="test-key", client=client), rag=rag, summary_dir=tmp_path)

    result = service.answer_question("video-1", "What is the fact?")

    assert result == {
        "answer": "Grounded answer",
        "sources": [{"start": 120.5, "end": 145.8, "text": "A real transcript fact"}],
    }


def test_question_without_context_does_not_call_llm(tmp_path: Path) -> None:
    client = FakeCompletions(["This must not be returned"])
    service = VideoAIService(llm=GroqLLMService(api_key="test-key", client=client), rag=FakeRAG([]), summary_dir=tmp_path)

    result = service.answer_question("video-1", "What is not in the transcript?")

    assert result == {
        "answer": "The answer cannot be determined from the video context.",
        "sources": [],
    }
    assert client.prompts == []


def test_llm_failures_are_normalized() -> None:
    class BrokenCompletions:
        def create(self, **kwargs):
            raise TimeoutError("request timed out")

    service = GroqLLMService(api_key="test-key", client=BrokenCompletions())

    with pytest.raises(RuntimeError, match="LLM request failed"):
        service.complete("question")