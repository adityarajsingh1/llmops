"""
A fake provider for tests, demos and local development: no network, no key.
It answers from a script, or echoes the last user message back.
"""

from __future__ import annotations

from collections.abc import Iterable

from llmops.providers.base import CompletionRequest, CompletionResponse, Provider, ProviderError, Usage
from llmops.providers.registry import register


def count_tokens(text: str) -> int:
    # rough, but stable: about what a real tokenizer gives for English
    return max(1, round(len(text.split()) * 1.3))


@register
class FakeProvider(Provider):
    name = "fake"

    def __init__(self, replies: Iterable[str | Exception] | None = None, prefix: str = "echo: "):
        self.replies = list(replies or [])
        self.prefix = prefix
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.requests.append(request)
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            text = reply
        else:
            last_user = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
            text = self.prefix + last_user
        if request.max_tokens is not None:
            text = " ".join(text.split()[: request.max_tokens])
        prompt = " ".join(m.content for m in request.messages)
        return CompletionResponse(
            text=text,
            model=request.model,
            provider=self.name,
            usage=Usage(prompt_tokens=count_tokens(prompt), completion_tokens=count_tokens(text)),
            finish_reason="stop",
        )


def failing(status: int, times: int = 1) -> list[Exception]:
    """Helper for tests: `times` errors like the given HTTP status."""
    return [ProviderError.from_status("fake", status, "simulated") for _ in range(times)]
