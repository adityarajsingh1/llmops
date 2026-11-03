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

**Early, and being built in the open.** Right now there's the skeleton: the FastAPI app, settings and a health check. I'll tick things off above as they land.

```bash
pip install -e ".[dev]"
llmops serve            # http://127.0.0.1:8000/health, API docs at /docs
```

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
