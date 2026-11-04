"""Anthropic Messages API."""

from __future__ import annotations

import os
from typing import Any

import httpx

from llmops.providers.base import CompletionRequest, CompletionResponse, Provider, ProviderError, Usage
from llmops.providers.http import HTTPProvider
from llmops.providers.registry import register

API_VERSION = "2023-06-01"
# the Messages API requires max_tokens, so this is used when the caller didn't set one
DEFAULT_MAX_TOKENS = 1024


@register
class AnthropicProvider(HTTPProvider, Provider):
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.anthropic.com/v1",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise ProviderError(
                "no Anthropic API key: pass api_key or set ANTHROPIC_API_KEY", provider=self.name
            )
        headers = {"x-api-key": key, "anthropic-version": API_VERSION}
        self.client = self._make_client(base_url, headers, timeout, transport)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        # Anthropic takes the system prompt as its own field, not as a message
        system = "\n\n".join(m.content for m in request.messages if m.role == "system")
        messages = [m.model_dump() for m in request.messages if m.role != "system"]
        if not messages:
            raise ProviderError("anthropic needs at least one user message", provider=self.name)

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": messages,
            "max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS,
        }
        if system:
            payload["system"] = system
        if request.temperature is not None:
            payload["temperature"] = min(request.temperature, 1.0)  # Anthropic's range is 0-1

        data = await self._post(self.client, "/messages", payload)
        text = "".join(
            block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
        )
        usage = data.get("usage") or {}
        return CompletionResponse(
            text=text,
            model=data.get("model", request.model),
            provider=self.name,
            usage=Usage(
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
            ),
            finish_reason=data.get("stop_reason"),
        )

    async def aclose(self) -> None:
        await self.client.aclose()
