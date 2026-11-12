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
- a response cache for repeated requests, exact and semantic
- per-team rate limits
- quality monitors on a sample of live answers, a weekly drift report and Prometheus alert rules
- offline evals for prompts, with a GitHub Action that fails a pull request when a prompt change makes the scores drop

```bash
pip install -e ".[dev]"
llmops serve            # http://127.0.0.1:8000/health, API docs at /docs
```

Or with Docker, together with Prometheus and Grafana:

```bash
docker compose up --build
curl localhost:8000/v1/chat/completions -H "Authorization: Bearer llmops-demo-key-do-not-use-in-prod" \
  -H "Content-Type: application/json" -d '{"model": "echo", "messages": [{"role": "user", "content": "hi"}]}'
```

The compose file starts with `gateway.example.yaml`, which has a demo team and the fake `echo` model, so it runs without any API keys. Point the volume at your own `gateway.yaml` and set `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` to use real models. The image runs as a non-root user, keeps the usage database in the `/data` volume, and has a health check on `/health`. Prometheus is at http://localhost:9090, already scraping the gateway, and Grafana at http://localhost:3000 (admin / admin, change it with `GRAFANA_ADMIN_PASSWORD`) opens on the llmops dashboard. The dashboard shows request rate, error rate, latency per model, tokens and cost per team, the cache hit ratio, provider errors, fallbacks and the circuit breakers, with filters for team and model:

![The llmops Grafana dashboard after a few demo requests to the fake echo model](docs/grafana-dashboard.png)

The dashboard is a plain JSON file in `deploy/grafana/dashboards/`, so it can be imported into an existing Grafana too.

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

### Caching

Plenty of LLM traffic repeats itself: the same FAQ question, the same ticket classified again after the app retried. With `cache: {enabled: true}` in `gateway.yaml`, an identical request is answered from memory, at no cost, with `x-llmops-cache: hit` on the response. Cache hits aren't recorded as usage and don't count against budgets.

Only requests with `temperature: 0` are cached unless `cache_nondeterministic` is on, because a caller who asked for a higher temperature wants different answers. Entries are kept per team unless `shared_across_teams` is on, so a team never gets an answer generated from another team's prompt. A model can opt out with `cache: false` on its entry. The cache lives in the gateway process; a Redis backend (implement `CacheBackend` in `cache.py`) would let several gateways share one.

The **semantic cache** (`cache.semantic.enabled`) also reuses an answer when a new question is close to an earlier one, and marks the response `x-llmops-cache: semantic-hit`. Everything except the last user message has to match exactly (same system prompt, same earlier turns, same settings). Only the last message is compared, by cosine similarity of embeddings, against `threshold` (0.9 by default). The embedder is pluggable:

- `hashing` (the default) is built in and needs no network. It hashes words and character trigrams, so it matches rewordings that use the same words ("How do I reset my password?" and "Reset my password, how do I do it?" score 0.93). It doesn't understand meaning, so paraphrases with different words won't match, and it keeps "...on mobile?" (0.86) apart from the original at the default threshold.
- `openai` uses an embedding model and matches real paraphrases.
- Others plug in through the `llmops.embedders` entry point group.

A wrong semantic hit means a wrong answer, so start with a high threshold and watch `llmops_cache_lookups_total{result="semantic_hit"}` before lowering it. If the embedder fails, the gateway logs it and carries on without the semantic cache.

### Rate limits

A team can get `rate_limit: {requests_per_minute: 120, burst: 20}`. It's a token bucket, so up to `burst` requests can go through back to back and the allowance refills evenly over the minute; one runaway loop in an app gets slowed down without punishing normal bursty traffic. Over the limit, requests get a 429 `rate_limit_exceeded` with a `Retry-After` header, and every response carries `x-ratelimit-limit-requests` and `x-ratelimit-remaining-requests` like OpenAI's API, so SDKs that back off on 429s just work. Limits are kept per gateway process. A tokens-per-minute limit would be a good addition.

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
| `llmops_cache_lookups_total` | model, result | cache lookups: hit, semantic_hit or miss |
| `llmops_monitor_checks_total` | monitor, result | monitor results on sampled live answers |

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

### Evals: catching prompt changes that make answers worse

A prompt edit that looks harmless can quietly break something: the model starts inventing prices again, or stops handing off questions it can't answer. Evals catch that before it ships. A suite in `evals/` lists cases for one prompt (the variables to fill in and checks the answer has to pass):

```yaml
prompt: support-reply
checks:                                # run on every case
  - {type: not_contains, value: "as an AI"}
cases:
  - id: no-made-up-prices
    variables:
      product: Acme Cloud
      article: "The Team plan adds shared workspaces and SSO. See the pricing page for current prices."
      question: How much does the Team plan cost?
    checks:
      - {type: regex, pattern: '[$€£]\s?\d', must_match: false}   # no price is in the article
      - {type: contains_any, values: ["pricing page", "colleague"]}
```

The built-in checks are `contains`, `not_contains`, `contains_any`, `regex` (with `must_match: false` for "must not match"), `max_words` and `json` (valid JSON, optionally with required keys). They're deterministic on purpose: cheap, no second model call, and the same answer always gets the same score. A new check is one class with `@register_scorer` in `llmops/evals/scorers.py`, or it can live in another package and plug in through the `llmops.scorers` entry point group. An LLM-as-judge scorer for things like tone would fit the same way.

A case's score is the share of its checks that passed, and a suite's score is the average over its cases.

```bash
llmops eval check evals/*.yaml                                   # validate, no model calls
llmops eval run evals/*.yaml --config gateway.yaml               # the active versions
llmops eval run evals/support-reply.yaml --version 1 --out v1.json
llmops eval run evals/support-reply.yaml --version 2 --out v2.json
llmops eval compare v1.json v2.json --max-drop 0.05              # exit code 1 if a score dropped more
```

Models are called through the gateway config, so evals get the same retries and fallbacks as live traffic. One suite works for every version of a prompt: a variable that only some versions use (like `article`) is left out for the others, while a misspelled variable still fails.

**In CI**, `.github/workflows/evals.yml` runs on pull requests that touch `prompts/` or `evals/`. It runs the suites against the base branch's prompts and against the PR's, then compares them. The PR fails if a suite's score drops by more than 5 points, and the comparison table goes into the job summary. It needs an `OPENAI_API_KEY` repository secret, with models listed in `evals/config/gateway.ci.yaml`. Without the secret (pull requests from forks never get one), it still validates the files and skips the model calls with a notice. Model answers vary a little from run to run, which is why there's an allowed drop rather than none.

### Monitoring live answers

Evals check a prompt change before it ships, but they can't notice a provider quietly changing the model behind the same name, or users asking things nobody wrote a test case for. For that, the gateway can keep a random sample of real answers (`monitoring.sample_rate`, off by default) and run monitors on them. A monitor uses the same checks as the eval suites and applies them to every sampled answer of one prompt or one model:

```yaml
monitoring:
  sample_rate: 0.05          # keep 5% of answers
  monitors:
    - name: support-no-made-up-prices
      prompt: support-reply
      checks:
        - {type: regex, pattern: '[$€£]\s?\d', must_match: false}
```

Results go to Prometheus right away as `llmops_monitor_checks_total{monitor, result}`, so a falling pass rate can alert. The samples are stored next to the usage records. Samples contain model output, so think about what your users send before you turn this on.

**The weekly report** compares the last 7 days with the 7 before, from the usage database:

```bash
llmops report                    # markdown: spend per team, cost and answer length per model, monitor pass rates
llmops report --fail-on-drift    # exit code 2 when something drifted, for a cron job that pings someone
llmops report --json
```

It flags a model whose cost per request or answer length moved by more than 25%, a monitor whose pass rate fell by more than 5 points (once both weeks have at least 20 samples), and a rise in answers cut off at `max_tokens`. These thresholds are simple on purpose, a starting point and not statistics, and with a handful of requests the numbers will be noisy.

**Alerts.** `deploy/prometheus/alerts.yml` has Prometheus rules for a gateway that's down, more than 5% of requests failing, a circuit breaker open for 5 minutes, p95 latency above 20s, more than 10% of a model's traffic going to fallbacks, teams getting 429s, and a monitor failing on more than 20% of sampled answers. The compose setup loads them, and CI unit-tests them with `promtool test rules`. Each alert links to a section of the runbook.

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
