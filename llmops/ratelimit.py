"""
Requests-per-minute limits per team, as token buckets: a team can send up to
`burst` requests at once, and the bucket refills at requests_per_minute / 60
per second. That smooths out one runaway script without punishing normal
bursty traffic.

The buckets live in the gateway process, so with several gateways each one
enforces the limit on its own share of the traffic.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field


class RateLimit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requests_per_minute: int = Field(gt=0)
    # requests allowed back to back; defaults to a tenth of a minute's worth (at least 1)
    burst: int | None = Field(default=None, gt=0)

    @property
    def capacity(self) -> int:
        return self.burst or max(1, self.requests_per_minute // 10)


class TokenBucket:
    def __init__(self, limit: RateLimit, clock: Callable[[], float] = time.monotonic):
        self.limit = limit
        self.clock = clock
        self.tokens = float(limit.capacity)
        self.updated = clock()

    def _refill(self) -> None:
        now = self.clock()
        rate = self.limit.requests_per_minute / 60
        self.tokens = min(self.limit.capacity, self.tokens + (now - self.updated) * rate)
        self.updated = now

    def take(self) -> bool:
        self._refill()
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False

    @property
    def remaining(self) -> int:
        self._refill()
        return int(self.tokens)

    def retry_after(self) -> int:
        """Whole seconds until the next request would be allowed."""
        self._refill()
        missing = max(0.0, 1 - self.tokens)
        # rounded first so float noise (10.0000001) doesn't add a whole second
        return max(1, math.ceil(round(missing * 60 / self.limit.requests_per_minute, 6)))
