import json
import logging
import re
from datetime import datetime, timezone

from backend.app.core.config import settings
from backend.app.core.log_context import job_id_var, request_id_var, user_id_var, video_id_var


_SENSITIVE_FIELDS = re.compile(r"(?i)(password|token|prompt|api[_-]?key)\s*[=:]\s*[^,\s]+")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        exception = self.formatException(record.exc_info) if record.exc_info else None
        configured_key = settings.groq_api_key
        if configured_key:
            message = message.replace(configured_key, "[REDACTED]")
            if exception:
                exception = exception.replace(configured_key, "[REDACTED]")
        message = _SENSITIVE_FIELDS.sub(r"\1=[REDACTED]", message)
        if exception:
            exception = _SENSITIVE_FIELDS.sub(r"\1=[REDACTED]", exception)

        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "request_id": request_id_var.get(),
            "user_id": user_id_var.get(),
            "video_id": video_id_var.get(),
            "job_id": job_id_var.get(),
        }
        if exception:
            payload["exception"] = exception
        for field in (
            "duration_ms",
            "model",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "stage",
            "stage_status",
            "success",
        ):
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value
        return json.dumps(payload, ensure_ascii=True, default=str)


def configure_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    if root_logger.handlers:
        for handler in root_logger.handlers:
            handler.setFormatter(JsonFormatter())
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        root_logger.addHandler(handler)
