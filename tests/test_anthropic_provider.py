import json

import httpx
import pytest

from llmops.providers import ChatMessage, CompletionRequest, ProviderError, available, create

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def anthropic_with(handler):
    return create("anthropic", api_key="sk-ant-test", transport=httpx.MockTransport(handler))


def ok(text="Hi!", **extra):
    body = {
        "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 20, "output_tokens": 4},
        **extra,
    }
    return httpx.Response(200, json=body)


async def test_system_prompt_moves_to_its_own_field():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return ok()

    req = CompletionRequest(
        model="claude-sonnet-4-5",
        messages=[
            ChatMessage(role="system", content="be brief"),
            ChatMessage(role="user", content="hello"),
        ],
        temperature=1.7,
    )
    r = await anthropic_with(handler).complete(req)
    body = seen["body"]
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert (
        seen["headers"]["x-api-key"] == "sk-ant-test" and seen["headers"]["anthropic-version"] == "2023-06-01"
    )
    assert body["system"] == "be brief"
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["max_tokens"] == 1024  # required by the API, so there's a default
    assert body["temperature"] == 1.0  # clamped to Anthropic's range
    assert (r.text, r.provider, r.finish_reason) == ("Hi!", "anthropic", "end_turn")
    assert (r.usage.prompt_tokens, r.usage.completion_tokens) == (20, 4)


async def test_text_blocks_are_joined():
    def handler(request):
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "Hello "}, {"type": "text", "text": "world"}]},
        )

    req = CompletionRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    assert (await anthropic_with(handler).complete(req)).text == "Hello world"


@pytest.mark.parametrize("status, retryable", [(529, True), (429, True), (400, False)])
async def test_errors(status, retryable):
    def handler(request):
        return httpx.Response(status, json={"type": "error", "error": {"type": "x", "message": "overloaded"}})

    req = CompletionRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    with pytest.raises(ProviderError, match="overloaded") as err:
        await anthropic_with(handler).complete(req)
    assert err.value.retryable is retryable


async def test_only_a_system_message_is_refused():
    req = CompletionRequest(model="m", messages=[ChatMessage(role="system", content="x")])
    with pytest.raises(ProviderError, match="at least one user message"):
        await anthropic_with(lambda r: ok()).complete(req)


def test_registered():
    assert {"anthropic", "fake", "openai"} <= set(available())
