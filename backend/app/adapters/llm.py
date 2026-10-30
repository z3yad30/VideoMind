import logging
import time
from typing import Any

from backend.app.core.config import settings


logger = logging.getLogger(__name__)


class GroqLLMClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = settings.groq_api_key if api_key is None else api_key
        self._client: Any | None = None

    def complete(self, messages: list[dict[str, str]], **opts: object) -> str:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not configured")
        if self._client is None:
            try:
                from groq import Groq
            except ImportError as exc:
                raise RuntimeError("groq is not installed") from exc
            self._client = Groq(api_key=self.api_key).chat.completions
        started_at = time.perf_counter()
        model = str(opts.get("model", settings.groq_model))
        try:
            response = self._client.create(messages=messages, **opts)
        except Exception as exc:
            logger.info(
                "LLM completion failed",
                extra={
                    "duration_ms": round((time.perf_counter() - started_at) * 1000),
                    "model": model,
                    "success": False,
                },
            )
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError("LLM request failed") from exc
        usage = getattr(response, "usage", None)
        logger.info(
            "LLM completion finished",
            extra={
                "duration_ms": round((time.perf_counter() - started_at) * 1000),
                "model": model,
                "input_tokens": getattr(usage, "prompt_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "success": True,
            },
        )
        return response.choices[0].message.content.strip()
