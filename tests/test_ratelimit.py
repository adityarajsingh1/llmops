import pytest
from fastapi.testclient import TestClient
from test_gateway import OPS, SEARCH, chat, no_sleep

from llmops.app import create_app
from llmops.auth import hash_key
from llmops.config import GatewayConfig
from llmops.gateway import Gateway
from llmops.providers.fake import FakeProvider
from llmops.ratelimit import RateLimit, TokenBucket


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def test_burst_then_refill():
    clock = Clock()
    bucket = TokenBucket(RateLimit(requests_per_minute=60, burst=3), clock=clock)
    assert [bucket.take() for _ in range(4)] == [True, True, True, False]
    assert bucket.retry_after() == 1
    clock.now += 1  # 60/min refills one per second
    assert bucket.take() and not bucket.take()
    clock.now += 100  # never more than the burst
    assert bucket.remaining == 3


def test_default_burst_is_a_tenth_of_a_minute():
    assert RateLimit(requests_per_minute=600).capacity == 60
    assert RateLimit(requests_per_minute=5).capacity == 1


def test_retry_after_for_slow_limits():
    clock = Clock()
    bucket = TokenBucket(RateLimit(requests_per_minute=2, burst=1), clock=clock)
    assert bucket.take() and not bucket.take()
    assert bucket.retry_after() == 30
    clock.now += 20
    assert bucket.retry_after() == 10


def limited_client(settings):
    data = {
        "providers": {"demo": {"type": "fake"}},
        "models": {"echo": {"provider": "demo"}},
        "teams": [
            {
                "name": "search",
                "key_sha256": hash_key(SEARCH),
                "rate_limit": {"requests_per_minute": 2, "burst": 2},
            },
            {"name": "ops", "key_sha256": hash_key(OPS)},
        ],
    }
    fake = FakeProvider()
    gateway = Gateway(GatewayConfig.model_validate(data), providers={"demo": fake}, sleep=no_sleep)
    return TestClient(create_app(settings, gateway)), fake


def test_over_the_limit_gets_a_429_with_retry_after(settings):
    client, fake = limited_client(settings)
    first, second, third = chat(client), chat(client), chat(client)
    assert first.status_code == second.status_code == 200
    assert first.headers["x-ratelimit-limit-requests"] == "2"
    assert first.headers["x-ratelimit-remaining-requests"] == "1"
    assert third.status_code == 429
    assert third.json()["error"]["code"] == "rate_limit_exceeded"
    assert int(third.headers["retry-after"]) >= 1
    assert third.headers["x-ratelimit-remaining-requests"] == "0"
    assert len(fake.requests) == 2


def test_limits_are_per_team(settings):
    client, _ = limited_client(settings)
    for _ in range(3):
        chat(client)
    r = chat(client, key=OPS)
    assert r.status_code == 200 and "x-ratelimit-limit-requests" not in r.headers


@pytest.mark.parametrize("bad", [{"requests_per_minute": 0}, {"requests_per_minute": 5, "burst": 0}, {}])
def test_bad_limits(bad):
    with pytest.raises(ValueError):
        RateLimit(**bad)
