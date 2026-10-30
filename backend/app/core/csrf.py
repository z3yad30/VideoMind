from urllib.parse import urlsplit

from backend.app.core.config import settings


def is_allowed_request_origin(origin: str | None, referer: str | None) -> bool:
    source = origin or referer
    if not source:
        return False
    parsed = urlsplit(source)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    normalized = f"{parsed.scheme}://{parsed.netloc}".rstrip("/").lower()
    return normalized in {item.lower() for item in settings.allowed_origins}
