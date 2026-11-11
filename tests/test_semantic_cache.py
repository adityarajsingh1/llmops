import httpx
import pytest
from fastapi.testclient import TestClient
from test_cache import answer
from test_gateway import OPS, SEARCH, chat, no_sleep

from llmops.app import create_app
from llmops.auth import hash_key
from llmops.cache import SemanticCache, SemanticConfig
from llmops.config import GatewayConfig
from llmops.embeddings import Embedder, HashingEmbedder, OpenAIEmbedder, cosine, create_embedder
from llmops.gateway import Gateway
from llmops.providers.fake import FakeProvider

pytestmark = pytest.mark.anyio
QUESTION = "How do I reset my password?"


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def similarity(a: str, b: str) -> float:
    e = HashingEmbedder()
    return cosine(await e.embed(a), await e.embed(b))


@pytest.mark.parametrize(
    "other, close",
    [
        ("how do i reset my password", True),  # case and punctuation
        ("Reset my password, how do I do it?", True),  # same words, other order
        ("How do I reset my password on mobile?", False),  # a different question
        ("How do I delete my account?", False),
    ],
)
async def test_hashing_embedder_against_the_default_threshold(other, close):
    # the hashing embedder only sees shared words, so these are the cases it can tell apart
    assert (await similarity(QUESTION, other) >= SemanticConfig().threshold) is close


async def test_semantic_cache_returns_the_closest_match():
    cache = SemanticCache(HashingEmbedder(), SemanticConfig(threshold=0.5), ttl_seconds=60)
    await cache.put("b", "How do I reset my password?", answer("reset"))
    await cache.put("b", "How do I delete my account?", answer("delete"))
    found, score = await cache.get("b", "how do I delete my account")
    assert found.response.text == "delete" and score > 0.9
    assert await cache.get("other-bucket", "how do I delete my account") is None


async def test_semantic_cache_expires_and_caps_buckets():
    now = [0.0]
    cache = SemanticCache(
        HashingEmbedder(), SemanticConfig(max_entries_per_bucket=2), ttl_seconds=10, clock=lambda: now[0]
    )
    for i in range(3):
        await cache.put("b", f"question number {i}", answer(str(i)))
    assert await cache.get("b", "question number 0") is None  # pushed out by the cap
    assert (await cache.get("b", "question number 2"))[0].response.text == "2"
    now[0] = 11
    assert await cache.get("b", "question number 2") is None


async def test_openai_embedder():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/embeddings" and request.headers["authorization"] == "Bearer sk-x"
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})

    e = create_embedder("openai", api_key="sk-x", transport=httpx.MockTransport(handler))
    assert isinstance(e, OpenAIEmbedder) and await e.embed("hi") == [0.1, 0.2]
    await e.aclose()


def test_unknown_embedder():
    with pytest.raises(ValueError, match="unknown embedder"):
        create_embedder("nope")


def semantic_client(settings, embedder=None):
    data = {
        "providers": {"demo": {"type": "fake"}},
        "models": {"echo": {"provider": "demo"}},
        "teams": [
            {"name": "search", "key_sha256": hash_key(SEARCH)},
            {"name": "ops", "key_sha256": hash_key(OPS)},
        ],
        "cache": {"enabled": True, "semantic": {"enabled": True}},
    }
    fake = FakeProvider()
    gateway = Gateway(
        GatewayConfig.model_validate(data), providers={"demo": fake}, sleep=no_sleep, embedder=embedder
    )
    return TestClient(create_app(settings, gateway)), fake


def ask(client, question, key=SEARCH, system="You are a support agent."):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": question}]
    return chat(client, key=key, messages=messages, temperature=0)


def test_close_question_gets_the_earlier_answer(settings):
    client, fake = semantic_client(settings)
    first = ask(client, QUESTION)
    second = ask(client, "how do i reset my password")
    assert second.headers["x-llmops-cache"] == "semantic-hit"
    assert (
        second.json()["choices"][0]["message"]["content"] == first.json()["choices"][0]["message"]["content"]
    )
    assert len(fake.requests) == 1


def test_no_semantic_hit_across_system_prompts_teams_or_questions(settings):
    client, fake = semantic_client(settings)
    ask(client, QUESTION)
    assert (
        ask(client, "how do i reset my password", system="Answer in French.").headers["x-llmops-cache"]
        == "miss"
    )
    assert ask(client, "how do i reset my password", key=OPS).headers["x-llmops-cache"] == "miss"
    assert ask(client, "How do I reset my password on mobile?").headers["x-llmops-cache"] == "miss"
    assert len(fake.requests) == 4


class BrokenEmbedder(Embedder):
    name = "broken"

    async def embed(self, text: str) -> list[float]:
        raise RuntimeError("embedding service down")


def test_a_broken_embedder_doesnt_break_requests(settings):
    client, fake = semantic_client(settings, embedder=BrokenEmbedder())
    assert ask(client, QUESTION).status_code == 200
    r = ask(client, QUESTION)  # the exact cache still works
    assert r.status_code == 200 and r.headers["x-llmops-cache"] == "hit"
