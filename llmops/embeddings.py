"""
Text embedders for the semantic cache. Each one turns a text into a vector,
and texts with similar meaning should get vectors pointing the same way.

`hashing` is built in and needs no network or model: it hashes words and
character trigrams into a fixed-size vector. It treats rewordings with the
same words (case, punctuation, word order, small typos) as close, but it
doesn't understand meaning, so paraphrases with different words don't
match. For that, use a real embedding model such as `openai`.
More can be added with @register_embedder or the `llmops.embedders` entry
point group.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from abc import ABC, abstractmethod
from importlib.metadata import entry_points
from typing import Any, ClassVar, TypeVar

import httpx

E = TypeVar("E", bound=type["Embedder"])
_EMBEDDERS: dict[str, type[Embedder]] = {}
_loaded_entry_points = False


class Embedder(ABC):
    name: ClassVar[str]

    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    async def aclose(self) -> None:
        return None


def register_embedder(cls: E) -> E:
    _EMBEDDERS[cls.name] = cls
    return cls


def create_embedder(name: str, **options: Any) -> Embedder:
    global _loaded_entry_points
    if not _loaded_entry_points:
        _loaded_entry_points = True
        for ep in entry_points(group="llmops.embedders"):
            _EMBEDDERS.setdefault(ep.name, ep.load())
    try:
        return _EMBEDDERS[name](**options)
    except KeyError:
        raise ValueError(f"unknown embedder '{name}'. available: {', '.join(sorted(_EMBEDDERS))}") from None


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


_WORD = re.compile(r"[a-z0-9]+")


@register_embedder
class HashingEmbedder(Embedder):
    name = "hashing"

    def __init__(self, dimensions: int = 1024):
        self.dimensions = dimensions

    def _features(self, text: str) -> list[str]:
        words = _WORD.findall(text.lower())
        grams = [w[i : i + 3] for w in words for i in range(max(1, len(w) - 2))]
        return [f"w:{w}" for w in words] + [f"g:{g}" for g in grams]

    async def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for feature in self._features(text):
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dimensions
            # a sign bit from the hash keeps unrelated features from adding up in one bucket
            vec[index] += 1.0 if digest[4] & 1 else -1.0
        return vec


@register_embedder
class OpenAIEmbedder(Embedder):
    name = "openai"

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        api_key: str | None = None,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        key = api_key or os.environ.get(api_key_env, "")
        if not key:
            raise ValueError(f"the openai embedder needs an API key: set {api_key_env}")
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
            transport=transport,
        )

    async def embed(self, text: str) -> list[float]:
        r = await self.client.post("/embeddings", json={"model": self.model, "input": text})
        r.raise_for_status()
        vector: list[float] = r.json()["data"][0]["embedding"]
        return vector

    async def aclose(self) -> None:
        await self.client.aclose()
