"""
Gateway settings. Everything can be set with environment variables prefixed
with LLMOPS_ (e.g. LLMOPS_PORT=9000) or in a .env file.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLMOPS_", env_file=".env", extra="ignore")

    environment: Literal["dev", "staging", "prod"] = "dev"
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    # shown on /health so you can tell gateways apart when running several
    instance_name: str = "llmops"


@lru_cache
def get_settings() -> Settings:
    return Settings()
