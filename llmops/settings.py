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
    # json for log collectors, text for reading in a terminal
    log_format: Literal["json", "text"] = "json"
    # OTLP/HTTP endpoint for traces, e.g. http://localhost:4318/v1/traces; unset means no export
    otel_endpoint: str | None = None
    # serve Prometheus metrics at /metrics
    metrics_enabled: bool = True
    # shown on /health so you can tell gateways apart when running several
    instance_name: str = "llmops"
    prompts_dir: str = "prompts"
    # providers, models and team keys; see gateway.example.yaml
    config: str = "gateway.yaml"
    # where token and cost records go: a SQLite file, or "memory" to keep them only until restart
    usage_db: str = "llmops-usage.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
