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
    search_llm_source: str = Field("groq", alias="SEARCH_LLM_SOURCE")
    search_main_model: str = Field("meta-llama/llama-4-scout-17b-16e-instruct", alias="SEARCH_MAIN_MODEL")
    guardrails_model: str = Field("llama-3.1-8b-instant", alias="GUARDRAILS_MODEL")
    search_temperature: float = Field(0.0, alias="SEARCH_TEMPERATURE")
    elastic_url: str = Field("http://localhost:9200", alias="ELASTIC_URL")
    # The alias, never a concrete index: rebuild_index swaps it atomically, so
    # nothing downstream needs to know whether it currently resolves to
    # recipes_v4 or recipes_v9. Defaulting to a concrete name is what made the
    # v2 -> v4 migration a config change in every environment.
    elastic_index: str = Field("recipes", alias="ELASTIC_INDEX")
    elastic_timeout: float = Field(3.0, alias="ELASTIC_TIMEOUT")
    cors_allow_origins: List[str] = Field(default_factory=lambda: ["*"], alias="CORS_ALLOW_ORIGINS")
    api_port: int = Field(8001, alias="PORT")
    profile_pipeline_version: str = Field("v1", alias="NUTRITION_PROFILE_PIPELINE_VERSION")
    recipe_cache_enabled: bool = Field(False, alias="RECIPE_CACHE_ENABLED")
    redis_url: str = Field("redis://localhost:6379", alias="REDIS_URL")
    redis_recipe_db: int = Field(7, alias="REDIS_RECIPE_DB")
    redis_recipe_ttl: int = Field(86400, alias="REDIS_RECIPE_TTL")

    # --- catalog layer ---------------------------------------------------- #
    # Aliases, never concrete indices: rebuild_index() swaps the alias
    # atomically, so nothing downstream needs to know about _v3/_v4.
    catalog_recipes_alias: str = Field("recipes", alias="CATALOG_RECIPES_ALIAS")
    catalog_profiles_alias: str = Field(
        "recipe_profiles", alias="CATALOG_PROFILES_ALIAS"
    )
    catalog_embedding_dim: int = Field(384, alias="ES_DIM")
    catalog_bulk_chunk_size: int = Field(500, alias="CATALOG_BULK_CHUNK_SIZE")
    # Serve FoodChat's meal-slot candidates from the catalog index rather than
    # Neo4j. On by default: the graph holds no annotations and no planning_tier,
    # so the Neo4j path cannot honour a cuisine preference or an exclusion from
    # planning however it is asked. Set to false to fall back if the ES path
    # misbehaves — the Neo4j implementation is kept, not deleted, for exactly
    # that reason.
    # Off by default until allergen parity with the Neo4j path is proven. The
    # graph excludes taxonomically (mozzarella is a descendant of dairy); the
    # index can only match the declared allergen plus the ingredient name, so it
    # is currently the weaker filter. Better recommendations are not worth a
    # weaker allergen exclusion.
    foodchat_candidates_from_elastic: bool = Field(
        False, alias="FOODCHAT_CANDIDATES_FROM_ELASTIC"
    )
    # The corpus is ~7k recipes after the recipe1m purge, so the whole of it
    # sits well inside one result window; this only needs to be revisited if a
    # corpus of a different order of magnitude is loaded again.
    elastic_max_result_window: int = Field(
        100000, alias="ELASTIC_MAX_RESULT_WINDOW"
    )

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

    @field_validator("elastic_url")
    def _validate_elastic_url(cls, value: str) -> str:  # noqa: N805
        if not value.startswith(("http://", "https://")):
            raise ValueError("ELASTIC_URL must start with http:// or https://")
        return value.rstrip("/")

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
