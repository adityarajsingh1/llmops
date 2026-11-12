# llmops

[![CI](https://github.com/adityarajsingh1/llmops/actions/workflows/ci.yml/badge.svg)](https://github.com/adityarajsingh1/llmops/actions/workflows/ci.yml)
![python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue)
![license](https://img.shields.io/badge/license-MIT-green)

A self-hosted, OpenAI-compatible gateway that sits between your apps and the LLM providers. It gives every team its own key and budget, keeps prompts versioned, survives provider outages, and tells you what you're spending and when answer quality starts slipping.

## Why

Once a company has a few apps calling LLM APIs, the same problems show up everywhere. Nobody knows what each team spends until the invoice arrives. Prompts are copy-pasted strings with no history. A provider outage takes every app down at once. And when answers quietly get worse after a prompt edit or a model update, nobody notices until a customer does.

I wanted one place that handles all of that, so the apps themselves stay simple. Apps keep using the OpenAI SDK they already have and only change the base URL.

## What it does

- **One OpenAI-compatible API** in front of OpenAI, Anthropic and any OpenAI-compatible server (Ollama, vLLM, ...), with model names you choose
- **Per-team API keys** (only hashes are stored), model allow-lists, **monthly budgets** that block or warn, and **rate limits**
- **Reliability:** retries with backoff, timeouts, fallback models and a circuit breaker per provider
- **Prompt registry:** versioned prompts in YAML, callable by name, with a CLI to diff and check them
- **Caching:** exact, plus an optional semantic cache with pluggable embedders
- **Cost accounting** per team and model, a `/v1/usage` endpoint and CLI reports
- **Observability:** Prometheus metrics, a Grafana dashboard, JSON logs and OpenTelemetry traces tied together by request ids, and alert rules with a runbook
- **Quality:** offline evals that fail a pull request when a prompt change makes answers worse, live monitors on sampled traffic, and a weekly drift report

## Architecture

```mermaid
flowchart TB
    apps["Your apps (OpenAI SDK)"] -->|"POST /v1/chat/completions"| auth

    subgraph gw["llmops gateway"]
        auth["team key"] --> limits["rate limit + budget"] --> cache{"cache"}
        cache -->|hit| done["response"]
        cache -->|miss| router["routing: retries, fallbacks,<br/>circuit breaker"]
        router --> done
        done --> store[("usage + samples<br/>(SQLite)")]
    end

    router --> providers["OpenAI · Anthropic · Ollama / vLLM / ..."]
    store --> reports["llmops usage, llmops report"]

    subgraph obs["observability"]
        direction TB
        prometheus["Prometheus + alert rules"] --> grafana["Grafana"]
        traces["Jaeger / Tempo (OTLP)"]
    end
    gw -.->|metrics, traces| obs
```

Prompts from `prompts/*.yaml` can be called by name instead of sending messages, and `llmops eval` runs the eval suites through the same routing. A request goes through in that order: the team's key is checked, then its rate limit and budget, then the cache. On a miss the router sends it to the model's provider, with retries and timeouts, and falls back to other models while the provider is failing. The answer is priced and recorded per team, and a share of answers can be sampled for the quality monitors. Metrics, logs and traces come out of every step.

The code is laid out the same way:

| module | what it does |
|---|---|
| `gateway.py` | the `/v1` API and the request flow above |
| `config.py` | `gateway.yaml`: providers, models, teams and the sections below |
| `auth.py`, `ratelimit.py`, `budgets.py` | keys, rate limits, budgets |
| `providers/` | the provider interface, OpenAI, Anthropic, a fake one, and the plugin registry |
| `reliability.py` | retries, timeouts, circuit breaker |
| `cache.py`, `embeddings.py` | exact and semantic cache, embedders |
| `prompts.py` | the prompt registry |
| `usage.py` | usage records and samples (SQLite or memory) |
| `metrics.py`, `observability.py` | Prometheus metrics, JSON logs, tracing |
| `evals/` | suites, scorers, runner, comparison |
| `monitoring.py`, `report.py` | live monitors, weekly report |
| `cli.py` | `llmops serve / prompts / keys / usage / eval / report` |

## Quick start

With Docker, together with Prometheus and Grafana:

```bash
docker compose up --build
curl localhost:8000/v1/chat/completions -H "Authorization: Bearer llmops-demo-key-do-not-use-in-prod" \
  -H "Content-Type: application/json" -d '{"model": "echo", "messages": [{"role": "user", "content": "hi"}]}'
```

That starts with `gateway.example.yaml`: a demo team and the fake `echo` model, so it runs without any API keys. Grafana is at http://localhost:3000 (admin / admin) with the llmops dashboard, and Prometheus at http://localhost:9090.

Or without Docker:

```bash
pip install -e ".[dev]"
cp gateway.example.yaml gateway.yaml
llmops serve            # http://127.0.0.1:8000, API docs at /docs
```

Then point an app at it:

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="llmops-demo-key-do-not-use-in-prod")
reply = client.chat.completions.create(model="echo", messages=[{"role": "user", "content": "hi"}])
```

To use real models, add them to `gateway.yaml`, set `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`, and give each team a key with `llmops keys new <team>`.

## Documentation

- [Configuration and features](docs/configuration.md): the endpoint and team keys, providers, the prompt registry, fallbacks, costs and budgets, caching, rate limits, settings
- [Observability](docs/observability.md): metrics, the Grafana dashboard, logs, traces and alerts
- [Quality](docs/quality.md): offline evals, the CI check for prompt changes, live monitors and the weekly report
- [Runbook](docs/runbook.md): what to do when an alert fires

## Status

All of the above works and is tested (with fake providers, so the test suite needs no API keys), and the Docker setup is smoke-tested in CI. It hasn't run in anyone's production yet, so expect rough edges. The main known limits are that the cache, rate limits and circuit breakers live in one gateway process, usage is in SQLite, and streaming responses aren't supported yet. Contributions in any of those directions are very welcome.

## Contributing

Contributions are very welcome, and the plugin design means a lot of them are a single file: a provider, an eval check, an embedder or a storage backend. [CONTRIBUTING.md](CONTRIBUTING.md) explains the setup (no API keys needed) and how to add each kind of plugin, and [docs/good-first-issues.md](docs/good-first-issues.md) lists well-scoped ideas to start with.

```bash
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
mypy llmops
```

CI runs these on Python 3.10 to 3.12, builds and smoke-tests the Docker image, and unit-tests the alert rules. Please report security problems privately, see [SECURITY.md](SECURITY.md).

## License

MIT
