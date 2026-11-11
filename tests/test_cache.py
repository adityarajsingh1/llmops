import pytest
from fastapi.testclient import TestClient
from test_gateway import OPS, SEARCH, chat, no_sleep

from llmops.app import create_app
from llmops.auth import hash_key
from llmops.cache import CacheConfig, CachedAnswer, MemoryCache, cache_key, is_cacheable
from llmops.config import GatewayConfig
from llmops.gateway import Gateway
from llmops.providers import ChatMessage, CompletionRequest, CompletionResponse
from llmops.providers.fake import FakeProvider, failing
from llmops.usage import MemoryUsageStore


def answer(text):
    return CachedAnswer(model="m", response=CompletionResponse(text=text, model="m", provider="p"))


def req(content="hi", temperature=0.0, **kw):
    return CompletionRequest(
        model="m", messages=[ChatMessage(role="user", content=content)], temperature=temperature, **kw
    )


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


def test_memory_cache_lru_and_ttl():
    clock = Clock()
    cache = MemoryCache(max_entries=2, ttl_seconds=10, clock=clock)
    cache.put("a", answer("A"))
    cache.put("b", answer("B"))
    assert cache.get("a").response.text == "A"  # a is now the most recently used
    cache.put("c", answer("C"))
    assert cache.get("b") is None and len(cache) == 2
    clock.now = 11
    assert cache.get("a") is None and cache.get("c") is None


def test_key_covers_everything_that_changes_the_answer():
    base = cache_key("search", "echo", req())
    assert base == cache_key("search", "echo", req())
    assert base != cache_key("ops", "echo", req())
    assert base != cache_key("search", "other", req())
    assert base != cache_key("search", "echo", req("hello"))
    assert base != cache_key("search", "echo", req(max_tokens=10))
    assert base != cache_key("search", "echo", req(temperature=0.5))


@pytest.mark.parametrize(
    "config, temperature, cacheable",
    [
        ({}, 0, False),  # off by default
        ({"enabled": True}, 0, True),
        ({"enabled": True}, 0.7, False),
        ({"enabled": True}, None, False),  # no temperature means the provider's default, usually 1
        ({"enabled": True, "cache_nondeterministic": True}, 0.7, True),
    ],
)
def test_is_cacheable(config, temperature, cacheable):
    assert is_cacheable(CacheConfig(**config), req(temperature=temperature)) is cacheable


def cached_client(settings, fake=None, **cache):
    data = {
        "providers": {"demo": {"type": "fake"}},
        "models": {
            "echo": {"provider": "demo", "price": {"input": 1, "output": 1}},
            "fresh": {"provider": "demo", "cache": False},
        },
        "teams": [
            {"name": "search", "key_sha256": hash_key(SEARCH)},
            {"name": "ops", "key_sha256": hash_key(OPS)},
        ],
        "cache": {"enabled": True, **cache},
    }
    fake = fake or FakeProvider()
    usage = MemoryUsageStore()
    gateway = Gateway(
        GatewayConfig.model_validate(data), providers={"demo": fake}, sleep=no_sleep, usage=usage
    )
    return TestClient(create_app(settings, gateway)), fake, usage


def test_second_identical_request_is_a_free_hit(settings):
    client, fake, usage = cached_client(settings)
    first = chat(client, temperature=0)
    second = chat(client, temperature=0)
    assert first.headers["x-llmops-cache"] == "miss" and second.headers["x-llmops-cache"] == "hit"
    assert second.json()["choices"] == first.json()["choices"]
    assert second.headers["x-llmops-cost-usd"] == "0.000000"
    assert len(fake.requests) == 1 and len(usage.records) == 1


def test_teams_dont_share_entries_by_default(settings):
    client, fake, _ = cached_client(settings)
    chat(client, temperature=0)
    assert chat(client, key=OPS, temperature=0).headers["x-llmops-cache"] == "miss"
    assert len(fake.requests) == 2

    client, fake, _ = cached_client(settings, shared_across_teams=True)
    chat(client, temperature=0)
    assert chat(client, key=OPS, temperature=0).headers["x-llmops-cache"] == "hit"


def test_uncacheable_requests_have_no_cache_header(settings):
    client, fake, _ = cached_client(settings)
    assert "x-llmops-cache" not in chat(client, temperature=0.7).headers
    assert "x-llmops-cache" not in chat(client, model="fresh", temperature=0).headers
    chat(client, model="fresh", temperature=0)
    assert len(fake.requests) == 3


def test_errors_are_not_cached(settings):
    client, fake, _ = cached_client(settings, fake=FakeProvider([*failing(400), "fine"]))
    assert chat(client, temperature=0).status_code == 502
    r = chat(client, temperature=0)
    assert r.status_code == 200 and r.headers["x-llmops-cache"] == "miss"


def test_cache_metrics(settings):
    from test_metrics import scrape, value

    client, _, _ = cached_client(settings)
    for _ in range(3):
        chat(client, temperature=0)
    m = scrape(client)
    assert value(m, "llmops_cache_lookups_total", model="echo", result="miss") == 1
    assert value(m, "llmops_cache_lookups_total", model="echo", result="hit") == 2
