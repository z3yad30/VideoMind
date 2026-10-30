from typing import Any

from backend.app.core.config import settings


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
        try:
            response = self._client.create(messages=messages, **opts)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("LLM request failed") from exc
        return response.choices[0].message.content.strip()
