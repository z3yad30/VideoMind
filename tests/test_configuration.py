from backend.app.core.config import Settings


def test_cookie_secure_defaults_follow_environment() -> None:
    assert Settings(_env_file=None, app_env="development").cookie_secure is False
    assert Settings(_env_file=None, app_env="production").cookie_secure is True
    assert Settings(_env_file=None, app_env="production", cookie_secure=False).cookie_secure is False


def test_allowed_origins_accept_comma_separated_environment_value() -> None:
    settings = Settings(
        _env_file=None,
        allowed_origins="https://app.example, http://localhost:5173/",
    )

    assert settings.allowed_origins == ("https://app.example", "http://localhost:5173")


def test_retrieval_improvements_are_disabled_by_default() -> None:
    settings = Settings(_env_file=None)

    assert settings.query_rewrite is False
    assert settings.hybrid is False
    assert settings.rerank is False
    assert settings.rerank_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
