"""Application configuration helpers."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=(Path(__file__).resolve().parent / ".env", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    neo4j_uri: str = Field(..., alias="NEO4J_URI")
    search_main_model: str = Field("openai/gpt-oss-20b", alias="SEARCH_MAIN_MODEL")
    guardrails_model: str = Field("llama-3.1-8b-instant", alias="GUARDRAILS_MODEL")
    search_temperature: float = Field(0.0, alias="SEARCH_TEMPERATURE")
    strict_value_mapping: bool = Field(True, alias="STRICT_VALUE_MAPPING")
    neo4j_connect_timeout: float = Field(5.0, alias="NEO4J_CONNECT_TIMEOUT")
    cors_allow_origins: List[str] = Field(default_factory=lambda: ["*"], alias="CORS_ALLOW_ORIGINS")

    @field_validator("neo4j_uri")
    def _validate_neo4j_uri(cls, value: str) -> str:  # noqa: N805
        if not value.startswith("bolt://"):
            raise ValueError("NEO4J_URI must start with bolt://")
        return value

    @field_validator("search_main_model", "guardrails_model", mode="before")
    def _strip_models(cls, value: Optional[str]):  # noqa: N805
        if isinstance(value, str):
            value = value.strip()
        return value

    @field_validator("cors_allow_origins", mode="before")
    def _parse_origins(cls, value):  # noqa: N805
        if value is None or value == "":
            return ["*"]
        if isinstance(value, str):
            items = [origin.strip() for origin in value.split(",")]
            return [origin for origin in items if origin]
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings instance."""

    return Settings()
