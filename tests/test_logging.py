import io
import json
import logging
from types import SimpleNamespace

from backend.app.adapters.llm import GroqLLMClient
from backend.app.core.logging_config import JsonFormatter


def test_llm_logs_usage_and_latency_without_api_key() -> None:
    fake_key = "gsk_fake-secret-for-log-test"
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=5, total_tokens=17),
    )

    class FakeCompletions:
        def create(self, **_kwargs):
            return response

    client = GroqLLMClient(api_key=fake_key)
    client._client = FakeCompletions()
    logger = logging.getLogger("backend.app.adapters.llm")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    previous_level = logger.level
    was_disabled = logger.disabled
    disabled_below = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    logger.disabled = False
    logger.setLevel(logging.INFO)
    try:
        assert client.complete([{"role": "user", "content": "private prompt"}]) == "answer"
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.disabled = was_disabled
        logging.disable(disabled_below)

    formatted = [json.loads(line) for line in stream.getvalue().splitlines()]
    llm_log = next(record for record in formatted if record["message"] == "LLM completion finished")
    assert llm_log["input_tokens"] == 12
    assert llm_log["output_tokens"] == 5
    assert llm_log["total_tokens"] == 17
    assert isinstance(llm_log["duration_ms"], int)
    assert fake_key not in stream.getvalue()
