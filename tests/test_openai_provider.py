import json

import httpx
import pytest

from llmops.providers import ChatMessage, CompletionRequest, ProviderError, create

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def openai_with(handler):
    return create("openai", api_key="sk-test", transport=httpx.MockTransport(handler))


REQ = CompletionRequest(
    model="gpt-5-mini",
    messages=[ChatMessage(role="system", content="be brief"), ChatMessage(role="user", content="hi")],
    max_tokens=50,
)


async def test_sends_the_right_request_and_parses_the_answer():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "gpt-5-mini-2025-08-07",
                "choices": [{"message": {"role": "assistant", "content": "Hello!"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
        )

    r = await openai_with(handler).complete(REQ)
    assert seen["url"] == "https://api.openai.com/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-test"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "be brief"}
    assert seen["body"]["max_completion_tokens"] == 50 and "temperature" not in seen["body"]
    assert (r.text, r.model, r.provider) == ("Hello!", "gpt-5-mini-2025-08-07", "openai")
    assert (r.usage.prompt_tokens, r.usage.completion_tokens) == (12, 3)


@pytest.mark.parametrize("status, retryable", [(429, True), (500, True), (401, False), (400, False)])
async def test_http_errors(status, retryable):
    def handler(request):
        return httpx.Response(status, json={"error": {"message": "nope", "type": "x"}})

    with pytest.raises(ProviderError) as err:
        await openai_with(handler).complete(REQ)
    assert err.value.status == status and err.value.retryable is retryable
    assert "nope" in str(err.value)


async def test_network_errors_are_retryable():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ProviderError, match="couldn't reach openai") as err:
        await openai_with(handler).complete(REQ)
    assert err.value.retryable


async def test_timeouts_are_retryable():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ProviderError, match="timed out") as err:
        await openai_with(handler).complete(REQ)
    assert err.value.retryable


def test_needs_a_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="no OpenAI API key"):
        create("openai")


async def test_compatible_servers_via_base_url():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    p = create(
        "openai", api_key="x", base_url="http://localhost:11434/v1", transport=httpx.MockTransport(handler)
    )
    assert (await p.complete(REQ)).text == "ok"
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    await p.aclose()
