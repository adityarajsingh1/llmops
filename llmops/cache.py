"""
Response cache. Lots of LLM traffic repeats itself (the same FAQ question,
the same classification of the same ticket after a retry in the app), and a
cached answer costs nothing and comes back in microseconds.

By default only deterministic requests (temperature 0) are cached, since for
anything else the caller asked for variety. Entries are kept per team unless
`shared_across_teams` is on, so one team can never get an answer that was
generated from another team's prompt.
"""

from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from llmops.providers import CompletionRequest, CompletionResponse


class CacheConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    ttl_seconds: float = Field(default=3600, gt=0)
    max_entries: int = Field(default=10_000, gt=0)
    # cache requests with a temperature above 0 (or none given) too
    cache_nondeterministic: bool = False
    shared_across_teams: bool = False


class CachedAnswer(BaseModel):
    model: str  # the model that answered, which after a fallback isn't the one asked for
    response: CompletionResponse


class CacheBackend(ABC):
    """Where entries live. MemoryCache is per process; a Redis backend would let several
    gateways share one cache (a good first contribution)."""

    @abstractmethod
    def get(self, key: str) -> CachedAnswer | None: ...

    @abstractmethod
    def put(self, key: str, value: CachedAnswer) -> None: ...


class MemoryCache(CacheBackend):
    """LRU with a time to live."""

    def __init__(self, max_entries: int, ttl_seconds: float, clock: Callable[[], float] = time.monotonic):
        self.max_entries = max_entries
        self.ttl = ttl_seconds
        self.clock = clock
        self._entries: OrderedDict[str, tuple[float, CachedAnswer]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: str) -> CachedAnswer | None:
        item = self._entries.get(key)
        if item is None:
            return None
        stored_at, value = item
        if self.clock() - stored_at > self.ttl:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return value

    def put(self, key: str, value: CachedAnswer) -> None:
        self._entries[key] = (self.clock(), value)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


def is_cacheable(config: CacheConfig, request: CompletionRequest) -> bool:
    return config.enabled and (config.cache_nondeterministic or request.temperature == 0)


def cache_key(scope: str, alias: str, request: CompletionRequest) -> str:
    """Everything that changes the answer goes into the key: who (unless shared), which model,
    the messages and the sampling settings."""
    data = {
        "scope": scope,
        "model": alias,
        "upstream": request.model,
        "messages": [m.model_dump() for m in request.messages],
        "max_tokens": request.max_tokens,
        "temperature": request.temperature,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
