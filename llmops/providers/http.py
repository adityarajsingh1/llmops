"""Shared bits for providers that talk HTTP: one client per provider, and errors mapped to ProviderError."""

from __future__ import annotations

from typing import Any, ClassVar

import httpx

from llmops.providers.base import ProviderError


class HTTPProvider:
    """Mixin with an httpx client. `transport` is there so tests can plug in httpx.MockTransport."""

    name: ClassVar[str]

    def _make_client(
        self,
        base_url: str,
        headers: dict[str, str],
        timeout: float,
        transport: httpx.AsyncBaseTransport | None,
    ) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout, transport=transport)

    async def _post(self, client: httpx.AsyncClient, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await client.post(path, json=payload)
        except httpx.TimeoutException:
            raise ProviderError(f"{self.name} timed out", provider=self.name, retryable=True) from None
        except httpx.TransportError as e:
            raise ProviderError(
                f"couldn't reach {self.name}: {e}", provider=self.name, retryable=True
            ) from None
        if r.status_code >= 400:
            raise ProviderError.from_status(self.name, r.status_code, _error_detail(r))
        data: dict[str, Any] = r.json()
        return data


def _error_detail(r: httpx.Response) -> str:
    # both OpenAI and Anthropic put a readable message in {"error": {"message": ...}}
    try:
        return str(r.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return r.text[:200] or r.reason_phrase
