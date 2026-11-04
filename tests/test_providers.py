import pytest

from llmops.providers import (
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderError,
    available,
    create,
    register,
)
from llmops.providers.fake import FakeProvider, failing

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def req(text="hello there", **kw):
    return CompletionRequest(model="test-model", messages=[ChatMessage(role="user", content=text)], **kw)


async def test_fake_echoes_and_counts_tokens():
    p = create("fake")
    r = await p.complete(req("how are you"))
    assert r.text == "echo: how are you" and r.provider == "fake" and r.model == "test-model"
    assert (
        r.usage.prompt_tokens > 0
        and r.usage.total_tokens == r.usage.prompt_tokens + r.usage.completion_tokens
    )


async def test_fake_scripted_replies_and_errors():
    p = FakeProvider(replies=["first", *failing(503)])
    assert (await p.complete(req())).text == "first"
    with pytest.raises(ProviderError) as err:
        await p.complete(req())
    assert err.value.retryable and err.value.status == 503
    assert len(p.requests) == 2


async def test_max_tokens_is_respected():
    r = await FakeProvider(replies=["one two three four"]).complete(req(max_tokens=2))
    assert r.text == "one two"


@pytest.mark.parametrize(
    "status, retryable", [(429, True), (500, True), (503, True), (400, False), (401, False)]
)
def test_which_errors_are_retryable(status, retryable):
    assert ProviderError.from_status("x", status, "detail").retryable is retryable


def test_request_validation():
    with pytest.raises(ValueError):
        CompletionRequest(model="m", messages=[])
    with pytest.raises(ValueError):
        req(temperature=5)


def test_registry():
    assert "fake" in available()
    with pytest.raises(ValueError, match="unknown provider 'nope'"):
        create("nope")


def test_register_a_custom_provider():
    @register
    class Shouty(Provider):
        name = "shouty-test"

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            return CompletionResponse(
                text=request.messages[-1].content.upper(), model=request.model, provider=self.name
            )

    assert isinstance(create("shouty-test"), Shouty)
    with pytest.raises(ValueError, match="already registered"):

        @register
        class Other(Shouty):
            name = "shouty-test"


def test_provider_needs_a_name():
    with pytest.raises(TypeError):

        @register
        class Nameless(Provider):
            async def complete(self, request):  # pragma: no cover
                raise NotImplementedError
