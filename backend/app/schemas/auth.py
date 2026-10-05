import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


_USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,32}$")


class CredentialsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(strict=True, min_length=1, max_length=64)
    password: str = Field(strict=True, min_length=1, max_length=1024)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if not _USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError("Username must be 1-32 letters, numbers, dots, underscores, or hyphens")
        return normalized

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Password cannot be empty")
        return value


class UserResponse(BaseModel):
    username: str