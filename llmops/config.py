"""
gateway.yaml: which providers exist, which model names the gateway serves, and
which teams may call it. See gateway.example.yaml for a commented example.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from llmops.budgets import Budget
from llmops.cache import CacheConfig
from llmops.providers import Provider, create
from llmops.reliability import ReliabilityConfig
from llmops.usage import Price


class ConfigError(Exception):
    pass


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    # read the key from this variable, so secrets stay out of the file
    api_key_env: str | None = None
    # passed to the provider as is, e.g. base_url or timeout
    options: dict[str, Any] = Field(default_factory=dict)

    def build(self) -> Provider:
        options = dict(self.options)
        if self.api_key_env:
            key = os.environ.get(self.api_key_env)
            if not key:
                raise ConfigError(f"environment variable {self.api_key_env} is not set")
            options["api_key"] = key
        return create(self.type, **options)


class ModelRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    # the provider's own name for the model; defaults to the name clients use
    upstream: str | None = None
    # other models (by their names here) to try, in order, when this one's provider is down
    fallbacks: list[str] = Field(default_factory=list)
    # USD per million tokens; without a price the model's calls are recorded with a cost of 0
    price: Price | None = None
    # set to false for models whose answers must always be fresh, even when caching is on
    cache: bool = True


class Team(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # models this team may use; empty means all of them
    models: list[str] = Field(default_factory=list)
    # no budget means no limit (usage is still recorded)
    budget: Budget | None = None

    def may_use(self, model: str) -> bool:
        return not self.models or model in self.models


class GatewayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    models: dict[str, ModelRoute] = Field(default_factory=dict)
    teams: list[Team] = Field(default_factory=list)
    reliability: ReliabilityConfig = Field(default_factory=ReliabilityConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)

    @model_validator(mode="after")
    def _check_references(self) -> GatewayConfig:
        for alias, route in self.models.items():
            if route.provider not in self.providers:
                raise ValueError(f"model '{alias}' uses provider '{route.provider}', which isn't defined")
            for fb in route.fallbacks:
                if fb == alias or fb not in self.models:
                    raise ValueError(f"model '{alias}' has an invalid fallback '{fb}'")
        names: set[str] = set()
        hashes: set[str] = set()
        for team in self.teams:
            if team.name in names:
                raise ValueError(f"team '{team.name}' is defined twice")
            if team.key_sha256 in hashes:
                raise ValueError(f"team '{team.name}' reuses another team's key")
            names.add(team.name)
            hashes.add(team.key_sha256)
            unknown = [m for m in team.models if m not in self.models]
            if unknown:
                raise ValueError(f"team '{team.name}' lists unknown model(s): {', '.join(unknown)}")
        return self

    @classmethod
    def load(cls, path: str | Path) -> GatewayConfig:
        path = Path(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            return cls.model_validate(data)
        except OSError as e:
            raise ConfigError(f"can't read {path}: {e.strerror}") from None
        except (yaml.YAMLError, ValidationError) as e:
            raise ConfigError(f"{path.name}: {e}") from None
