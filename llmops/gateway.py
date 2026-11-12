"""
The OpenAI-compatible API: POST /v1/chat/completions and GET /v1/models.

Apps that already use an OpenAI SDK only change base_url and api_key. Errors
come back in OpenAI's shape too ({"error": {"message", "type", "code"}}), so
the SDKs raise their usual exceptions.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import random
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from opentelemetry import trace
from pydantic import BaseModel, ConfigDict, Field, model_validator

from llmops import budgets
from llmops.auth import bearer_token, find_owner
from llmops.cache import (
    CacheBackend,
    CachedAnswer,
    MemoryCache,
    SemanticCache,
    cache_key,
    is_cacheable,
    semantic_bucket,
)
from llmops.config import ConfigError, GatewayConfig, Team
from llmops.embeddings import Embedder, create_embedder
from llmops.metrics import Metrics
from llmops.monitoring import Sample, run_monitors
from llmops.observability import request_id_var
from llmops.prompts import PromptError, PromptRegistry
from llmops.providers import ChatMessage, CompletionRequest, CompletionResponse, Provider, ProviderError
from llmops.ratelimit import TokenBucket
from llmops.reliability import CircuitBreaker, Sleep, call_with_retries
from llmops.usage import MemoryUsageStore, UsageRecord, UsageStore

log = logging.getLogger("llmops.gateway")
tracer = trace.get_tracer("llmops")


class GatewayError(Exception):
    def __init__(
        self,
        status: int,
        message: str,
        code: str,
        type: str = "invalid_request_error",
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.type = type
        self.headers = headers or {}


def error_response(status: int, message: str, code: str, type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": {"message": message, "type": type, "code": code}}
    )


async def gateway_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, GatewayError)
    response = error_response(exc.status, exc.message, exc.code, exc.type)
    response.headers.update(exc.headers)
    return response


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
    msg = str(first.get("msg", "invalid request")).removeprefix("Value error, ")
    message = f"{where}: {msg}" if where else msg
    return error_response(400, message, "invalid_request")


# --- request and response bodies -------------------------------------------------------------


class WireMessage(BaseModel):
    # "developer" is what newer OpenAI SDKs send instead of "system"
    role: Literal["system", "developer", "user", "assistant"]
    content: str

    def to_chat(self) -> ChatMessage:
        return ChatMessage(role="system" if self.role == "developer" else self.role, content=self.content)


class PromptRef(BaseModel):
    """llmops extension: use a prompt from the registry instead of sending messages."""

    model_config = ConfigDict(extra="forbid")

    name: str
    version: int | None = None
    variables: dict[str, Any] = Field(default_factory=dict)


class ChatCompletionRequest(BaseModel):
    # other OpenAI fields (top_p, seed, ...) are accepted and ignored for now
    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    messages: list[WireMessage] | None = None
    prompt: PromptRef | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    stream: bool = False

    @model_validator(mode="after")
    def _messages_or_prompt(self) -> ChatCompletionRequest:
        if (self.messages is None) == (self.prompt is None):
            raise ValueError("send either messages or prompt, not both")
        if self.messages is not None and not self.messages:
            raise ValueError("messages can't be empty")
        if self.prompt is None and not self.model:
            raise ValueError("model is required")
        return self


class AssistantMessage(BaseModel):
    role: Literal["assistant"] = "assistant"
    content: str


class Choice(BaseModel):
    index: int = 0
    message: AssistantMessage
    finish_reason: str | None


class WireUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletion(BaseModel):
    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[Choice]
    usage: WireUsage


class ModelUsage(BaseModel):
    model: str
    requests: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float


class TeamUsage(BaseModel):
    object: Literal["usage"] = "usage"
    team: str
    month: str
    spent_usd: float
    budget_usd: float | None
    remaining_usd: float | None
    models: list[ModelUsage]


class ModelCard(BaseModel):
    id: str
    object: Literal["model"] = "model"
    owned_by: str


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelCard]


# --- the gateway ------------------------------------------------------------------------------


class Gateway:
    """Config, prompts and live provider clients. Providers are built on first use, so a missing
    key for one provider only breaks the models that use it."""

    def __init__(
        self,
        config: GatewayConfig,
        prompts: PromptRegistry | None = None,
        providers: dict[str, Provider] | None = None,
        sleep: Sleep = asyncio.sleep,
        usage: UsageStore | None = None,
        cache: CacheBackend | None = None,
        embedder: Embedder | None = None,
        sampler: Callable[[], float] = random.random,
    ):
        self.config = config
        self.prompts = prompts or PromptRegistry([])
        self._providers: dict[str, Provider] = dict(providers or {})
        # one breaker per provider: an outage hits every model on it
        self.breakers = {name: CircuitBreaker(config.reliability.breaker) for name in config.providers}
        self._sleep = sleep
        self.usage = usage or MemoryUsageStore()
        self.metrics = Metrics()
        self._sampler = sampler
        self._buckets = {t.name: TokenBucket(t.rate_limit) for t in config.teams if t.rate_limit}
        self.cache = cache or MemoryCache(config.cache.max_entries, config.cache.ttl_seconds)
        self.semantic: SemanticCache | None = None
        sem = config.cache.semantic
        if config.cache.enabled and sem.enabled:
            embedder = embedder or create_embedder(sem.embedder, **sem.options)
            self.semantic = SemanticCache(embedder, sem, config.cache.ttl_seconds)
        for name, breaker in self.breakers.items():
            self.metrics.watch_breaker(name, breaker)
        unpriced = sorted(alias for alias, route in config.models.items() if route.price is None)
        if unpriced:
            log.warning(
                "models without a price count as free in usage and budgets", extra={"models": unpriced}
            )

    def authenticate(self, authorization: str | None) -> Team:
        key = bearer_token(authorization)
        if key is None:
            raise GatewayError(401, "missing API key: send 'Authorization: Bearer <key>'", "invalid_api_key")
        team = find_owner(key, self.config.teams)
        if not isinstance(team, Team):
            raise GatewayError(401, "invalid API key", "invalid_api_key")
        return team

    def provider(self, name: str) -> Provider:
        if name not in self._providers:
            try:
                self._providers[name] = self.config.providers[name].build()
            except (ConfigError, ProviderError, ValueError, TypeError) as e:
                raise GatewayError(
                    500, f"provider '{name}' is misconfigured: {e}", "provider_misconfigured", "api_error"
                ) from None
        return self._providers[name]

    def build_request(self, body: ChatCompletionRequest, team: Team) -> tuple[str, CompletionRequest]:
        """The model name the client asked for, and the request to send upstream."""
        if body.prompt is not None:
            try:
                req = self.prompts.render(body.prompt.name, body.prompt.variables, body.prompt.version)
            except PromptError as e:
                raise GatewayError(400, str(e), "prompt_error") from None
            alias = body.model or req.model
        else:
            assert body.messages is not None and body.model is not None
            alias = body.model
            req = CompletionRequest(model=alias, messages=[m.to_chat() for m in body.messages])

        route = self.config.models.get(alias)
        if route is None:
            raise GatewayError(404, f"the model '{alias}' does not exist", "model_not_found")
        if not team.may_use(alias):
            raise GatewayError(
                403, f"team '{team.name}' may not use '{alias}'", "model_not_allowed", "permission_error"
            )

        updates: dict[str, Any] = {"model": route.upstream or alias}
        max_tokens = body.max_completion_tokens or body.max_tokens
        if max_tokens is not None:
            updates["max_tokens"] = max_tokens
        if body.temperature is not None:
            updates["temperature"] = body.temperature
        return alias, req.model_copy(update=updates)

    async def complete(self, alias: str, request: CompletionRequest) -> tuple[str, CompletionResponse]:
        """Send the request to `alias`, then to its fallbacks while the failure is the provider's.
        Returns the model that answered and its response."""
        last: Exception | None = None
        for candidate in [alias, *self.config.models[alias].fallbacks]:
            route = self.config.models[candidate]
            req = request.model_copy(update={"model": route.upstream or candidate})
            if candidate != alias:
                self.metrics.fallbacks.labels(model=alias, fallback=candidate).inc()
                log.warning(
                    "falling back", extra={"model": alias, "fallback": candidate, "reason": str(last)}
                )
            # span names and attributes follow the OpenTelemetry GenAI conventions
            with tracer.start_as_current_span(f"chat {req.model}", kind=trace.SpanKind.CLIENT) as span:
                span.set_attribute("gen_ai.operation.name", "chat")
                span.set_attribute("gen_ai.system", route.provider)
                span.set_attribute("gen_ai.request.model", req.model)
                span.set_attribute("llmops.model", candidate)
                span.set_attribute("llmops.fallback", candidate != alias)
                started = time.perf_counter()
                try:
                    provider = self.provider(route.provider)
                    resp = await call_with_retries(
                        functools.partial(provider.complete, req),
                        provider=route.provider,
                        policy=self.config.reliability.retry,
                        breaker=self.breakers[route.provider],
                        sleep=self._sleep,
                        on_error=functools.partial(self._count_error, route.provider),
                    )
                except (ProviderError, GatewayError) as e:
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    # the request itself is bad: another model would reject it too
                    if isinstance(e, ProviderError) and e.status in (400, 413, 422):
                        raise GatewayError(502, str(e), "provider_error", "api_error") from None
                    last = e
                    continue
                finally:
                    self.metrics.provider_latency.labels(provider=route.provider, model=candidate).observe(
                        time.perf_counter() - started
                    )
                span.set_attribute("gen_ai.response.model", resp.model)
                span.set_attribute("gen_ai.usage.input_tokens", resp.usage.prompt_tokens)
                span.set_attribute("gen_ai.usage.output_tokens", resp.usage.completion_tokens)
                return candidate, resp
        if isinstance(last, GatewayError):
            raise last
        assert isinstance(last, ProviderError)
        if last.retryable:
            raise GatewayError(503, str(last), "provider_unavailable", "api_error")
        raise GatewayError(502, str(last), "provider_error", "api_error")

    async def complete_cached(
        self, team: Team, alias: str, request: CompletionRequest
    ) -> tuple[str, CompletionResponse, Literal["hit", "semantic-hit", "miss"] | None]:
        """complete(), answered from the cache when possible. The last value says how, or is
        None when the request isn't cacheable."""
        if not (self.config.models[alias].cache and is_cacheable(self.config.cache, request)):
            return (*await self.complete(alias, request), None)
        scope = "*" if self.config.cache.shared_across_teams else team.name
        key = cache_key(scope, alias, request)
        hit = self.cache.get(key)
        if hit is not None:
            self.metrics.cache.labels(model=alias, result="hit").inc()
            return hit.model, hit.response, "hit"

        bucket, question = semantic_bucket(scope, alias, request), request.messages[-1].content
        if self.semantic is not None:
            try:
                near = await self.semantic.get(bucket, question)
            except Exception:
                # the embedder being down shouldn't take requests down with it
                log.exception("semantic cache lookup failed")
                near = None
            if near is not None:
                self.metrics.cache.labels(model=alias, result="semantic_hit").inc()
                return near[0].model, near[0].response, "semantic-hit"

        self.metrics.cache.labels(model=alias, result="miss").inc()
        answered_by, resp = await self.complete(alias, request)
        answer = CachedAnswer(model=answered_by, response=resp)
        self.cache.put(key, answer)
        if self.semantic is not None:
            try:
                await self.semantic.put(bucket, question, answer)
            except Exception:
                log.exception("semantic cache store failed")
        return answered_by, resp, "miss"

    def _count_error(self, provider: str, error: ProviderError) -> None:
        self.metrics.provider_errors.labels(provider=provider, retryable=str(error.retryable).lower()).inc()

    def check_rate_limit(self, team: Team) -> dict[str, str]:
        """Takes one request from the team's bucket. Returns OpenAI-style rate limit headers,
        or raises a 429 with Retry-After when the bucket is empty."""
        bucket = self._buckets.get(team.name)
        if bucket is None:
            return {}
        allowed = bucket.take()
        headers = {
            "x-ratelimit-limit-requests": str(bucket.limit.requests_per_minute),
            "x-ratelimit-remaining-requests": str(bucket.remaining),
        }
        if not allowed:
            wait = bucket.retry_after()
            raise GatewayError(
                429,
                f"team '{team.name}' is over its limit of {bucket.limit.requests_per_minute} requests per "
                f"minute; retry in {wait}s",
                "rate_limit_exceeded",
                "requests",
                headers={**headers, "retry-after": str(wait)},
            )
        return headers

    def check_budget(self, team: Team, now: datetime | None = None) -> budgets.BudgetStatus | None:
        """The team's budget status, or a 429 if the budget is used up and set to block."""
        if team.budget is None:
            return None
        spent = self.usage.spend(team.name, budgets.month_start(now))
        status = budgets.check(team.budget, spent)
        if status.exceeded:
            if team.budget.on_exceed == "block":
                raise GatewayError(
                    429,
                    f"team '{team.name}': {status.warning}",
                    "budget_exceeded",
                    "insufficient_quota",
                )
            log.warning("team over budget", extra={"team": team.name, "spent_usd": round(spent, 4)})
        return status

    def record_usage(self, team: Team, alias: str, resp: CompletionResponse) -> UsageRecord:
        """Write the usage of one answered request, priced by the model that answered."""
        route = self.config.models[alias]
        rec = UsageRecord(
            team=team.name,
            model=alias,
            provider=route.provider,
            prompt_tokens=resp.usage.prompt_tokens,
            completion_tokens=resp.usage.completion_tokens,
            cost_usd=route.price.cost(resp.usage) if route.price else 0.0,
            request_id=request_id_var.get(),
        )
        self.usage.record(rec)
        self.metrics.observe_usage(
            rec.team, rec.model, rec.prompt_tokens, rec.completion_tokens, rec.cost_usd
        )
        return rec

    def maybe_sample(
        self, team: Team, alias: str, resp: CompletionResponse, prompt: PromptRef | None
    ) -> Sample | None:
        """Keep this answer for monitoring, with probability `monitoring.sample_rate`."""
        cfg = self.config.monitoring
        if cfg.sample_rate == 0 or self._sampler() >= cfg.sample_rate:
            return None
        sample = Sample(
            team=team.name,
            model=alias,
            prompt=prompt.name if prompt else None,
            prompt_version=self.prompts[prompt.name].get(prompt.version).version if prompt else None,
            output=resp.text,
            completion_tokens=resp.usage.completion_tokens,
            finish_reason=resp.finish_reason,
        )
        sample.checks = run_monitors(cfg.monitors, sample)
        for name, passed in sample.checks.items():
            self.metrics.monitor_checks.labels(monitor=name, result="pass" if passed else "fail").inc()
        self.usage.record_sample(sample)
        return sample

    async def aclose(self) -> None:
        for p in self._providers.values():
            await p.aclose()
        if self.semantic is not None:
            await self.semantic.embedder.aclose()
        self.usage.close()


router = APIRouter(prefix="/v1", tags=["openai-compatible"])


def _gateway(request: Request) -> Gateway:
    gateway: Gateway = request.app.state.gateway
    return gateway


@router.post("/chat/completions", response_model=ChatCompletion)
async def chat_completions(
    body: ChatCompletionRequest, request: Request, response: Response
) -> ChatCompletion:
    gateway = _gateway(request)
    started = time.perf_counter()
    status = 200
    try:
        return await _chat_completion(gateway, body, request, response)
    except GatewayError as e:
        status = e.status
        raise
    finally:
        # labels only ever hold configured teams and models, so a stranger can't create new
        # series by sending made-up names
        model = getattr(request.state, "model", None)
        if model is None:
            model = body.model if body.model in gateway.config.models else "unknown"
        team = getattr(request.state, "team", "unknown")
        gateway.metrics.observe_request(team, model, status, time.perf_counter() - started)


async def _chat_completion(
    gateway: Gateway, body: ChatCompletionRequest, request: Request, response: Response
) -> ChatCompletion:
    team = gateway.authenticate(request.headers.get("authorization"))
    request.state.team = team.name
    response.headers.update(gateway.check_rate_limit(team))
    if body.stream:
        raise GatewayError(400, "streaming isn't supported yet; send stream: false", "unsupported")
    alias, upstream = gateway.build_request(body, team)
    request.state.model = alias
    budget = gateway.check_budget(team)
    # `model` in the response is the one that answered, which differs from the request after a fallback
    alias, resp, cache = await gateway.complete_cached(team, alias, upstream)
    if cache is not None:
        response.headers["x-llmops-cache"] = cache
    if cache in ("hit", "semantic-hit"):
        # nothing was spent, so nothing goes into usage or budgets
        response.headers["x-llmops-cost-usd"] = "0.000000"
    else:
        rec = gateway.record_usage(team, alias, resp)
        gateway.maybe_sample(team, alias, resp, body.prompt)
        response.headers["x-llmops-cost-usd"] = f"{rec.cost_usd:.6f}"
    if budget is not None:
        # as of before this request; the next one will see this request's cost too
        response.headers["x-llmops-budget-remaining-usd"] = f"{budget.remaining_usd:.4f}"
        if budget.warning:
            response.headers["x-llmops-budget-warning"] = budget.warning
    return ChatCompletion(
        id=f"chatcmpl-{uuid.uuid4().hex}",
        created=int(time.time()),
        model=alias,
        choices=[Choice(message=AssistantMessage(content=resp.text), finish_reason=resp.finish_reason)],
        usage=WireUsage(
            prompt_tokens=resp.usage.prompt_tokens,
            completion_tokens=resp.usage.completion_tokens,
            total_tokens=resp.usage.total_tokens,
        ),
    )


@router.get("/models", response_model=ModelList)
async def list_models(request: Request) -> ModelList:
    gateway = _gateway(request)
    team = gateway.authenticate(request.headers.get("authorization"))
    return ModelList(
        data=[
            ModelCard(id=alias, owned_by=route.provider)
            for alias, route in sorted(gateway.config.models.items())
            if team.may_use(alias)
        ]
    )


@router.get("/usage", response_model=TeamUsage)
async def team_usage(
    request: Request, month: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}$")
) -> TeamUsage:
    """What the calling team spent in a month (default: this one), per model. llmops extension."""
    gateway = _gateway(request)
    team = gateway.authenticate(request.headers.get("authorization"))
    try:
        start, end = budgets.month_range(month)
    except ValueError as e:
        raise GatewayError(400, str(e), "invalid_request") from None
    rows = gateway.usage.summary(start, end, team=team.name)
    spent = sum(r.cost_usd for r in rows)
    budget = team.budget.monthly_usd if team.budget else None
    return TeamUsage(
        team=team.name,
        month=start.strftime("%Y-%m"),
        spent_usd=round(spent, 6),
        budget_usd=budget,
        remaining_usd=None if budget is None else round(max(0.0, budget - spent), 6),
        models=[ModelUsage(**r.model_dump(exclude={"team"})) for r in rows],
    )
