"""OpenAI chat completions (also works with OpenAI-compatible servers through base_url)."""

from __future__ import annotations

import os
from typing import Any

import httpx

from llmops.providers.base import CompletionRequest, CompletionResponse, Provider, ProviderError, Usage
from llmops.providers.http import HTTPProvider
from llmops.providers.registry import register


@register
class OpenAIProvider(HTTPProvider, Provider):
    name = "openai"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        key = api_key or os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise ProviderError("no OpenAI API key: pass api_key or set OPENAI_API_KEY", provider=self.name)
        self.client = self._make_client(base_url, {"Authorization": f"Bearer {key}"}, timeout, transport)

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [m.model_dump() for m in request.messages],
        }
        # max_completion_tokens is what newer models (gpt-5, o-series) accept; older ones take it too
        if request.max_tokens is not None:
            payload["max_completion_tokens"] = request.max_tokens
        if request.temperature is not None:
            payload["temperature"] = request.temperature

        data = await self._post(self.client, "/chat/completions", payload)
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        return CompletionResponse(
            text=choice["message"].get("content") or "",
            model=data.get("model", request.model),
            provider=self.name,
            usage=Usage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
            ),
            finish_reason=choice.get("finish_reason"),
        )

    async def aclose(self) -> None:
        await self.client.aclose()
