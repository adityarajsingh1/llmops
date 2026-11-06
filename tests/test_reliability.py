import asyncio

import pytest

from llmops.providers import ProviderError
from llmops.reliability import (
    BreakerOpen,
    BreakerPolicy,
    CircuitBreaker,
    RetryPolicy,
    call_with_retries,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Script:
    """An async callable that fails or answers in a given order."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        if out == "hang":
            await asyncio.sleep(10)
        return out


def err(status: int) -> ProviderError:
    return ProviderError.from_status("p", status, "x")


async def run(script, policy=None, breaker=None):
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    result = await call_with_retries(
        script,
        provider="p",
        policy=policy or RetryPolicy(attempts=3, base_delay=1, timeout=None),
        breaker=breaker or CircuitBreaker(BreakerPolicy()),
        sleep=sleep,
    )
    return result, slept


def test_backoff_grows_and_is_capped():
    p = RetryPolicy(base_delay=0.5, max_delay=3)
    assert [p.delay(i, rng=lambda: 1.0) for i in range(5)] == [0.5, 1.0, 2.0, 3, 3]
    assert p.delay(2, rng=lambda: 0.5) == 1.0


async def test_retries_then_succeeds():
    script = Script(err(429), err(503), "ok")
    result, slept = await run(script)
    assert result == "ok" and script.calls == 3 and len(slept) == 2


async def test_gives_up_with_the_last_error():
    script = Script(err(500), err(502), err(503))
    with pytest.raises(ProviderError, match="503"):
        await run(script)


async def test_non_retryable_is_not_retried():
    script = Script(err(401), "never")
    breaker = CircuitBreaker(BreakerPolicy(failure_threshold=1))
    with pytest.raises(ProviderError, match="401"):
        await run(script, breaker=breaker)
    assert script.calls == 1 and breaker.state == "closed"


async def test_timeout_counts_as_retryable():
    script = Script("hang", "ok")
    result, _ = await run(script, policy=RetryPolicy(attempts=2, base_delay=0, timeout=0.01))
    assert result == "ok"


async def test_breaker_opens_and_refuses_calls():
    clock = Clock()
    breaker = CircuitBreaker(BreakerPolicy(failure_threshold=2, reset_seconds=30), clock=clock)
    with pytest.raises(BreakerOpen):
        await run(Script(err(503), err(503), "unused"), breaker=breaker)
    assert breaker.state == "open"

    script = Script("ok")
    with pytest.raises(BreakerOpen):
        await run(script, breaker=breaker)
    assert script.calls == 0


def test_half_open_lets_one_trial_through():
    clock = Clock()
    breaker = CircuitBreaker(BreakerPolicy(failure_threshold=1, reset_seconds=10), clock=clock)
    breaker.record_failure()
    assert not breaker.allow()
    clock.now = 10
    assert breaker.state == "half-open"
    assert breaker.allow() and not breaker.allow()  # only one trial at a time

    breaker.record_failure()  # the trial failed: open again for another 10s
    assert breaker.state == "open"
    clock.now = 20
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state == "closed" and breaker.failures == 0
