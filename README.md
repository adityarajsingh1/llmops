# llmops

[![CI](https://github.com/adityarajsingh1/llmops/actions/workflows/ci.yml/badge.svg)](https://github.com/adityarajsingh1/llmops/actions/workflows/ci.yml)
![license](https://img.shields.io/badge/license-MIT-green)

A self-hosted gateway that sits between your apps and the LLM providers (OpenAI, Anthropic, ...).

## Why

Once a team has a few apps calling LLM APIs, the same problems show up everywhere: nobody knows what each team is spending until the invoice arrives, prompts are copy-pasted strings with no versions, a provider outage takes everything down, and when answer quality quietly gets worse after a prompt or model change, nobody notices. I wanted one place that handles all of that, so the apps themselves stay simple.

The plan for llmops:

- an OpenAI-compatible endpoint, so existing apps switch by changing the base URL
- pluggable providers with retries, timeouts and fallbacks
- a prompt registry with versioned templates
- per-team API keys, token and cost accounting, and budgets
- structured logs, tracing and Prometheus metrics, with a Grafana dashboard
- offline evals that run in CI and fail a pull request when a prompt change makes quality drop
- caching, rate limits and drift monitoring

## Status

**Early, and being built in the open.** What works so far:

- the FastAPI app, settings and a health check
- the provider layer: one interface with OpenAI, Anthropic and a fake provider (for tests and demos), plus a plugin registry
- the prompt registry: versioned prompts in YAML with a CLI to list, show, diff and check them
- an OpenAI-compatible `/v1/chat/completions` endpoint with an API key per team
- retries with backoff, timeouts, fallback models and a circuit breaker per provider
- JSON logs and OpenTelemetry traces, tied together by a request id
- token and cost accounting per team and model, with a usage endpoint and a `llmops usage` report
- monthly budgets per team that block requests or just warn once they're used up
- Prometheus metrics at `/metrics`

```bash
pip install -e ".[dev]"
llmops serve            # http://127.0.0.1:8000/health, API docs at /docs
```

### The gateway endpoint

Any app that already talks to OpenAI can point at llmops instead. Copy `gateway.example.yaml` to `gateway.yaml`, then:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="llmops-demo-key-do-not-use-in-prod")
reply = client.chat.completions.create(model="echo", messages=[{"role": "user", "content": "hi"}])
```

`gateway.yaml` says which providers exist, which model names clients may ask for and where each one goes, and which teams have keys. Model names are yours to choose, so moving `support-bot` from one model or provider to another is a config change, not a deploy of every app.

Each team gets its own key and, optionally, a list of models it may use:

```bash
llmops keys new search      # prints the key (give it to the team) and the hash for gateway.yaml
```

Only the SHA-256 of a key is stored, so the config file can live in git without leaking working keys. Provider keys come from environment variables (`api_key_env`), and a missing one only breaks the models that use that provider.

Instead of `messages`, a request can name a prompt from the registry and pass its variables. The model defaults to the one the prompt version was written for:

```json
{"prompt": {"name": "support-reply", "variables": {"product": "Acme", "article": "...", "question": "..."}}}
```

Errors use OpenAI's format, so SDKs raise their usual exceptions: 401 for a bad key, 403 for a model the team can't use, 404 for an unknown model, 400 for a bad request, and 503 (worth retrying) or 502 (not) when the provider fails. Streaming isn't supported yet.

### When a provider fails

Every call to a provider gets a timeout and is retried with exponential backoff (with jitter, so a fleet of gateways doesn't retry in lockstep) when the error is worth retrying: 429s, 5xx, timeouts and network errors. A bad request or a bad key isn't retried.

If a model still fails, the gateway tries its `fallbacks` in order, so a request for `gpt-5-mini` can be answered by `claude-haiku` during an OpenAI outage. The response's `model` field says which model actually answered. A request the provider rejected as invalid (400, 413, 422) isn't sent to the fallbacks, since they would reject it too.

Each provider has a circuit breaker. After 5 failures in a row it opens and the gateway stops calling that provider for 30 seconds, so requests go straight to the fallback instead of waiting on timeouts. Then one trial request is let through, and a success closes the breaker again. All of these numbers are under `reliability:` in `gateway.yaml`.

### Costs and budgets

Give a model a price in `gateway.yaml` (USD per million input and output tokens, as on the provider's pricing page) and every answered request is recorded with its team, model, tokens and cost. After a fallback, the cost is the price of the model that actually answered. Records go to a SQLite file (`LLMOPS_USAGE_DB`, default `llmops-usage.db`), which is enough for one gateway. Several gateways sharing usage would need a networked store; `UsageStore` in `usage.py` is the interface to implement.

```yaml
models:
  gpt-5-mini: {provider: openai, price: {input: 0.25, output: 2.0}}   # check your provider's real prices
teams:
  - name: search
    key_sha256: ...
    budget: {monthly_usd: 200, on_exceed: block, warn_at: 0.8}
```

A team's budget resets at the start of each month (UTC). From `warn_at` (80% by default), responses carry an `x-llmops-budget-warning` header. Once the budget is used up, requests fail with a 429 `insufficient_quota` error, like OpenAI's, or with `on_exceed: warn` they still go through with the warning. Every response also has `x-llmops-cost-usd`, and `x-llmops-budget-remaining-usd` for teams with a budget. Spend is checked before each call, because the cost is only known afterwards, so a team can go over by about one request.

To see the numbers:

```bash
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:8000/v1/usage            # your team, this month
curl -H "Authorization: Bearer $KEY" "http://127.0.0.1:8000/v1/usage?month=2026-10"
llmops usage --month 2026-11                                                    # every team, from the database
```

```
team             model                     requests   tokens in  tokens out    cost USD
---------------------------------------------------------------------------------------
demo             echo                             2           2           6      0.0000
```

### Metrics

`/metrics` serves Prometheus metrics. It isn't behind a team key, so keep it on an internal network (or turn it off with `LLMOPS_METRICS_ENABLED=false`).

| metric | labels | what it counts |
|---|---|---|
| `llmops_requests_total` | team, model, status | chat requests by outcome (200, 401, 429, 503, ...) |
| `llmops_request_duration_seconds` | model | time to answer, retries and fallbacks included |
| `llmops_tokens_total` | team, model, kind | prompt and completion tokens |
| `llmops_cost_usd_total` | team, model | cost of answered requests |
| `llmops_provider_duration_seconds` | provider, model | time spent on each model, all its tries together |
| `llmops_provider_errors_total` | provider, retryable | failed tries, including ones a retry later hid |
| `llmops_fallbacks_total` | model, fallback | requests that went to a fallback model |
| `llmops_circuit_state` | provider | 0 closed, 1 half-open, 2 open |

Labels only ever contain teams and models from `gateway.yaml`. A request for a model that doesn't exist is counted as `model="unknown"`, so nobody can blow up the number of series by sending random names.

A few queries I find useful:

```promql
# p95 latency per model over 5 minutes
histogram_quantile(0.95, sum by (model, le) (rate(llmops_request_duration_seconds_bucket[5m])))

# share of requests that failed on the provider side
sum(rate(llmops_requests_total{status=~"502|503"}[5m])) / sum(rate(llmops_requests_total[5m]))

# spend per team today
sum by (team) (increase(llmops_cost_usd_total[1d]))

# retries that are hiding a struggling provider
sum by (provider) (rate(llmops_provider_errors_total{retryable="true"}[5m]))
```

Counters reset when the gateway restarts, which `rate()` and `increase()` handle. For exact monthly numbers, use `llmops usage`, which reads the usage database.

### Logs and traces

Every request gets an id. If the caller sends an `X-Request-ID` header, the gateway keeps it, otherwise it makes one, and either way it's returned in the response headers. That id is in every log line written while the request runs and on its trace, so when an app reports "this call was slow" you can find exactly what happened.

Logs are one JSON object per line on stderr, ready for Loki, Elasticsearch or CloudWatch (`LLMOPS_LOG_FORMAT=text` for reading them in a terminal). Each request writes one line with its team, model, status and duration:

```json
{"ts": "2026-11-06T07:51:13.347+00:00", "level": "info", "logger": "llmops", "message": "request", "request_id": "demo-1", "method": "POST", "path": "/v1/chat/completions", "status": 200, "duration_ms": 4.8, "team": "demo", "model": "echo"}
```

Traces use OpenTelemetry. Each request is a span, and each call to a provider is a child span with the model and token counts, following the OpenTelemetry GenAI conventions, so a fallback shows up as a failed span followed by one for the backup model. To send them to Jaeger, Tempo or any OTLP collector:

```bash
pip install -e ".[otel]"
LLMOPS_OTEL_ENDPOINT=http://localhost:4318/v1/traces llmops serve
```

Without that setting the spans are still created, but they cost next to nothing and go nowhere. API keys and prompt text are never logged.

### Providers

Every provider implements one small interface (`complete(request) -> response`) and reports errors as `ProviderError` with a `retryable` flag, so the gateway can decide whether to retry or fall back. 429s, 5xx and network errors are retryable; a bad key or a bad request isn't.

```python
from llmops.providers import ChatMessage, CompletionRequest, create

openai = create("openai")                     # OPENAI_API_KEY from the environment
claude = create("anthropic")                  # ANTHROPIC_API_KEY
local = create("openai", api_key="x", base_url="http://localhost:11434/v1")  # any OpenAI-compatible server

req = CompletionRequest(model="gpt-5-mini", messages=[ChatMessage(role="user", content="hi")])
resp = await openai.complete(req)             # resp.text, resp.usage.prompt_tokens, ...
```

A new provider is one class with `@register`. It can also live in its own package and plug in through the `llmops.providers` entry point group, without touching this repo.

### Prompt registry

Prompts live in `prompts/*.yaml` under version control instead of as strings inside app code. Each prompt has numbered versions and an `active` one, so a new version can be added and evaluated before it's switched on, and rolled back by changing one line:

```yaml
name: support-reply
active: 2
versions:
  - version: 1
    model: gpt-5-mini
    messages:
      - {role: system, content: "You are a support agent for {product}."}
      - {role: user, content: "{question}"}
  - version: 2
    note: answer only from the article, to stop made-up features
    ...
```

```bash
llmops prompts list                        # every prompt, * marks the active version
llmops prompts show support-reply          # the active version and its variables
llmops prompts diff support-reply 1 2      # what changed
llmops prompts check                       # validate all files, exit code 1 on errors (for CI)
```

Broken files (a typo'd placeholder, an `active` version that doesn't exist, versions out of order) fail when they're loaded, with the file name in the error, not when a customer hits that prompt. Missing or misspelled variables at render time raise an error too, instead of sending `{question}` to the model.

### Settings

Settings come from `LLMOPS_*` environment variables or a `.env` file (see `.env.example`). Bad values (a port of 70000, an unknown environment) fail at startup with a clear message instead of later.

## Development

```bash
pytest
ruff check . && ruff format --check .
mypy llmops
```

CI runs all of these on Python 3.10 to 3.12. A contributing guide is coming once there's more to contribute to, but issues and ideas are welcome already.

## License

MIT
