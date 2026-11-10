"""
Prometheus metrics, served at /metrics.

Each Gateway has its own registry rather than using the global default one,
so two apps in one process (tests, mostly) don't count into each other.
Labels are limited to things the config defines (teams, models, providers)
to keep the number of series small.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

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

    def observe_request(self, team: str, model: str, status: int, seconds: float) -> None:
        self.requests.labels(team=team, model=model, status=str(status)).inc()
        self.latency.labels(model=model).observe(seconds)

    def observe_usage(self, team: str, model: str, prompt: int, completion: int, cost: float) -> None:
        self.tokens.labels(team=team, model=model, kind="prompt").inc(prompt)
        self.tokens.labels(team=team, model=model, kind="completion").inc(completion)
        self.cost.labels(team=team, model=model).inc(cost)

    def render(self) -> bytes:
        return generate_latest(self.registry)
