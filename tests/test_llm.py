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
    def __init__(self, chunks, raw_transcript="No raw transcript is available."):
        self.chunks = chunks
        self.raw_transcript = raw_transcript

    def retrieve_relevant_chunks(self, video_id, question, top_k):
        return self.chunks

    def get_transcript_context_for_question(self, video_id, question):
        return self.raw_transcript


def test_groq_default_model_and_grounded_question_prompt() -> None:
    client = FakeCompletions(["The answer cannot be determined from the video context."])
    service = GroqLLMService(api_key="test-key", client=client)

    assert service.answer("What happened?", []) == "The answer cannot be determined from the video context."
    assert service.model == "openai/gpt-oss-120b"
    assert len(client.prompts) == 1
    assert "RAW VIDEO TRANSCRIPT:" in client.prompts[0]


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


def test_raw_transcript_fallback_calls_the_llm_with_transcript_context(tmp_path: Path) -> None:
    client = FakeCompletions(["The raw transcript explains the topic."])
    rag = FakeRAG([], "[1.0-2.0] A raw transcript fact")
    service = VideoAIService(llm=GroqLLMService(api_key="test-key", client=client), rag=rag, summary_dir=tmp_path)

    result = service.answer_question("video-1", "What is not in the transcript?")

    assert result["answer"] == "The raw transcript explains the topic."
    assert result["sources"] == []
    assert "RAW VIDEO TRANSCRIPT:" in client.prompts[0]
    assert "A raw transcript fact" in client.prompts[0]
    assert "Use the raw transcript to determine whether the question can be answered." in client.prompts[0]


def test_conversation_history_is_bounded_and_isolated_by_video(tmp_path: Path) -> None:
    client = FakeCompletions(["answer 1", "answer 2", "answer 3", "answer 4", "answer 5"])
    service = VideoAIService(llm=GroqLLMService(api_key="test-key", client=client), rag=FakeRAG([]), summary_dir=tmp_path)

    service.answer_question("video-a", "Question 1")
    service.answer_question("video-a", "Question 2")
    service.answer_question("video-a", "Question 3")
    service.answer_question("video-a", "Question 4")

    prompt = client.prompts[-1]
    assert "Question 2" in prompt
    assert "Question 3" in prompt
    assert "Question 4" in prompt
    assert "Question 1" not in prompt

    service.answer_question("video-b", "Question B")
    prompt_b = client.prompts[-1]
    assert "Question 1" not in prompt_b
    assert "Question 2" not in prompt_b
    assert "Question B" in prompt_b


def test_llm_failures_are_normalized() -> None:
    class BrokenCompletions:
        def create(self, **kwargs):
            raise TimeoutError("request timed out")

    service = GroqLLMService(api_key="test-key", client=BrokenCompletions())

    with pytest.raises(RuntimeError, match="LLM request failed"):
        service.complete("question")