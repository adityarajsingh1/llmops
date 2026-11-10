"""
Retries, timeouts and a circuit breaker around provider calls.

The gateway uses these so one slow or failing provider doesn't take every app
down with it: a call is retried a few times with backoff, each try has a
timeout, and a provider that keeps failing is skipped for a while (the
breaker is "open") so requests go straight to the fallback model instead of
waiting on timeouts.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from llmops.providers import ProviderError

T = TypeVar("T")
Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # total tries per model, the first one included
    attempts: int = Field(default=3, ge=1, le=10)
    base_delay: float = Field(default=0.25, ge=0)
    max_delay: float = Field(default=4.0, ge=0)
    # seconds per try; None waits as long as the provider's own client does
    timeout: float | None = Field(default=30.0, gt=0)

    def delay(self, retry: int, rng: Callable[[], float] = random.random) -> float:
        """Exponential backoff with full jitter: random between 0 and base * 2^retry, capped."""
        return min(self.max_delay, self.base_delay * 2**retry) * rng()


class BreakerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # consecutive failures that open the breaker
    failure_threshold: int = Field(default=5, ge=1)
    # how long an open breaker skips the provider before letting one trial call through
    reset_seconds: float = Field(default=30.0, gt=0)


class ReliabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    breaker: BreakerPolicy = Field(default_factory=BreakerPolicy)


class CircuitBreaker:
    """Closed: calls go through. Open: calls are refused until reset_seconds pass.
    Half-open: one trial call; success closes the breaker, failure opens it again."""

    def __init__(self, policy: BreakerPolicy, clock: Clock = time.monotonic):
        self.policy = policy
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None
        self._trial_running = False

    @property
    def state(self) -> Literal["closed", "open", "half-open"]:
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.policy.reset_seconds:
            return "half-open"
        return "open"

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half-open" and not self._trial_running:
            self._trial_running = True
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self._trial_running = False

    def record_failure(self) -> None:
        self.failures += 1
        if self._trial_running or self.failures >= self.policy.failure_threshold:
            self.opened_at = self.clock()
        self._trial_running = False


class BreakerOpen(ProviderError):
    def __init__(self, provider: str):
        message = f"{provider} is failing, skipped for now (circuit open)"
        super().__init__(message, provider=provider, retryable=True)


async def call_with_retries(
    call: Callable[[], Awaitable[T]],
    *,
    provider: str,
    policy: RetryPolicy,
    breaker: CircuitBreaker,
    sleep: Sleep = asyncio.sleep,
    on_error: Callable[[ProviderError], None] | None = None,
) -> T:
    """Run `call`, retrying retryable ProviderErrors with backoff. Raises the last error.
    `on_error` sees every failed try, including the ones a retry later hides (for metrics)."""
    last: ProviderError | None = None
    for attempt in range(policy.attempts):
        if attempt:
            await sleep(policy.delay(attempt - 1))
        if not breaker.allow():
            raise BreakerOpen(provider) from last
        try:
            if policy.timeout is None:
                result = await call()
            else:
                result = await asyncio.wait_for(call(), policy.timeout)
        except asyncio.TimeoutError:
            message = f"{provider} timed out after {policy.timeout}s"
            last = ProviderError(message, provider=provider, retryable=True)
        except ProviderError as e:
            last = e
        else:
            breaker.record_success()
            return result
        if on_error is not None:
            on_error(last)
        # a non-retryable error (a bad request, a bad key) says nothing about the provider's health
        if not last.retryable:
            raise last
        breaker.record_failure()
    assert last is not None
    raise last
