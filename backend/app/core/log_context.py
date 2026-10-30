from contextvars import ContextVar


request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
video_id_var: ContextVar[str | None] = ContextVar("video_id", default=None)
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)


def clear_log_context() -> None:
    request_id_var.set(None)
    user_id_var.set(None)
    video_id_var.set(None)
    job_id_var.set(None)
