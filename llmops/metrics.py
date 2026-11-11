"""
Prometheus metrics, served at /metrics.

Each Gateway has its own registry rather than using the global default one,
so two apps in one process (tests, mostly) don't count into each other.
Labels are limited to things the config defines (teams, models, providers)
to keep the number of series small.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

from llmops.reliability import CircuitBreaker

# LLM calls are slow compared to normal HTTP, so the buckets go up to a minute
LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "llmops_requests",
            "Chat completion requests, by outcome",
            ["team", "model", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "llmops_request_duration_seconds",
            "Time to answer a chat completion request, retries and fallbacks included",
            ["model"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.tokens = Counter(
            "llmops_tokens",
            "Tokens used, by kind (prompt or completion)",
            ["team", "model", "kind"],
            registry=self.registry,
        )
        self.cost = Counter(
            "llmops_cost_usd", "Cost of answered requests in USD", ["team", "model"], registry=self.registry
        )

        self.provider_latency = Histogram(
            "llmops_provider_duration_seconds",
            "Time spent on one model, all its tries together",
            ["provider", "model"],
            buckets=LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.provider_errors = Counter(
            "llmops_provider_errors",
            "Failed tries against a provider, including ones a retry later hid",
            ["provider", "retryable"],
            registry=self.registry,
        )
        self.fallbacks = Counter(
            "llmops_fallbacks",
            "Requests that went to a fallback model",
            ["model", "fallback"],
            registry=self.registry,
        )
        self.circuit = Gauge(
            "llmops_circuit_state",
            "Circuit breaker per provider: 0 closed, 1 half-open, 2 open",
            ["provider"],
            registry=self.registry,
        )

        self.cache = Counter(
            "llmops_cache_lookups",
            "Response cache lookups by result",
            ["model", "result"],
            registry=self.registry,
        )

    def watch_breaker(self, provider: str, breaker: CircuitBreaker) -> None:
        """Read the breaker's state at scrape time, so the gauge is never stale."""
        codes = {"closed": 0, "half-open": 1, "open": 2}
        self.circuit.labels(provider=provider).set_function(lambda: codes[breaker.state])

    def observe_request(self, team: str, model: str, status: int, seconds: float) -> None:
        self.requests.labels(team=team, model=model, status=str(status)).inc()
        self.latency.labels(model=model).observe(seconds)

    def observe_usage(self, team: str, model: str, prompt: int, completion: int, cost: float) -> None:
        self.tokens.labels(team=team, model=model, kind="prompt").inc(prompt)
        self.tokens.labels(team=team, model=model, kind="completion").inc(completion)
        self.cost.labels(team=team, model=model).inc(cost)

    def render(self) -> bytes:
        return generate_latest(self.registry)
