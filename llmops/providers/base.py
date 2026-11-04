"""
The provider interface. A provider turns a CompletionRequest into a
CompletionResponse by calling some LLM API. Everything else in the gateway
(retries, budgets, metrics) only talks to this interface, so adding a new
provider means writing one class.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class CompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class CompletionResponse(BaseModel):
    text: str
    model: str
    provider: str
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None


class ProviderError(Exception):
    """A failed provider call. `retryable` tells the gateway whether trying again could help."""

    def __init__(self, message: str, *, provider: str, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.provider = provider
        self.status = status
        self.retryable = retryable

    @classmethod
    def from_status(cls, provider: str, status: int, detail: str) -> ProviderError:
        # 408/429 and 5xx are worth retrying (or falling back); anything else is our fault or final
        retryable = status in (408, 409, 429) or status >= 500
        return cls(
            f"{provider} returned {status}: {detail}", provider=provider, status=status, retryable=retryable
        )


class Provider(ABC):
    """Base class for providers. Subclasses set `name` and implement `complete`."""

    name: ClassVar[str]

    @abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse: ...

    async def aclose(self) -> None:
        """Release connections. Providers without any don't need to override this."""
        return None
