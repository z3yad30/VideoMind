from urllib.parse import urlsplit

import pytest
from httpx import AsyncClient

from backend.app.core.rate_limit import rate_limiter


@pytest.fixture(autouse=True)
def reset_test_rate_limits(monkeypatch: pytest.MonkeyPatch):
    rate_limiter.reset()
    original_init = AsyncClient.__init__

    def init_with_browser_origin(self, *args, **kwargs):
        base_url = kwargs.get("base_url")
        if base_url and urlsplit(str(base_url)).hostname == "test":
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("Origin", "http://localhost:5173")
            kwargs["headers"] = headers
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(AsyncClient, "__init__", init_with_browser_origin)
    yield
    rate_limiter.reset()
