# Contributing

Thanks for wanting to help! llmops is meant to be easy to extend: providers, eval checks, embedders and storage backends are all small plugin classes, so a lot of useful contributions are one file plus its tests.

## Setup

```bash
git clone https://github.com/<you>/llmops
cd llmops
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install        # optional: runs ruff on every commit
pytest
```

You don't need any API keys. The tests use `FakeProvider` (`llmops/providers/fake.py`), which answers from a list you give it, can fail on cue (`failing(503, times=2)`) and records every request it got. HTTP providers are tested against `httpx.MockTransport`, so nothing goes over the network.

To run the gateway locally: `cp gateway.example.yaml gateway.yaml && llmops serve`, or `docker compose up --build` for the whole stack with Prometheus and Grafana.

## Finding your way around

The [architecture section of the README](README.md#architecture) shows how a request flows and which module does what. The [docs](docs/) explain every feature from the user's side, which is a good way to understand what the code is for before changing it.

If you're looking for something to work on, [docs/good-first-issues.md](docs/good-first-issues.md) has a list of well-scoped ideas.

## Before you open a PR

- `pytest`, `ruff check .`, `ruff format --check .` and `mypy llmops` pass. CI runs the same on Python 3.10 to 3.12.
- New behaviour has a test. A bug fix has a test that fails without the fix.
- User-facing changes are in the docs (`docs/*.md`) and, if they add config, in `gateway.example.yaml`. A test loads the example config, so it has to stay valid.
- If you change the alert rules, update `deploy/prometheus/alerts.test.yml` and run `promtool test rules` (CI does it in Docker).
- One feature or fix per PR. Small PRs get reviewed much faster.

## Adding things

**A provider.** Subclass `Provider` (`llmops/providers/base.py`), set `name`, implement `async complete(request) -> CompletionResponse`, and raise `ProviderError` with `retryable=True` for errors that are worth retrying (429s, 5xx, timeouts). `HTTPProvider` in `providers/http.py` does the error mapping for HTTP APIs. Register it with `@register`, or ship it as a separate package through the `llmops.providers` entry point group. `providers/openai.py` is a good template, and `tests/test_openai_provider.py` shows how to test one.

**An eval check (scorer).** Subclass `Scorer` in `llmops/evals/scorers.py` with a `name`, a pydantic `Params` model (that's what users write in YAML, so give fields clear names) and `score(output, params) -> CheckResult`. Decorate it with `@register_scorer`. The same checks run in eval suites and in live monitors, so keep them fast and deterministic, or document clearly when they aren't.

**An embedder for the semantic cache.** Subclass `Embedder` in `llmops/embeddings.py` and decorate it with `@register_embedder`, or use the `llmops.embedders` entry point group.

**A storage backend.** `UsageStore` (`usage.py`) and `CacheBackend` (`cache.py`) are abstract classes. The in-memory and SQLite versions are the reference; a new backend should pass the same tests (the store tests are parametrized, so add it to the fixture).

**A metric.** Add it in `metrics.py` with labels that only ever hold values from the config (teams, models, providers). Never put something a client sends straight into a label, or anyone can create unlimited series. Then document it in the table in `docs/observability.md`.

## Reporting bugs

Open an issue with what you ran, what you expected and what happened, plus your llmops version (`llmops version`) and Python version. Include the relevant part of `gateway.yaml`, but **never paste API keys or team keys**. A `request_id` from the response headers helps a lot when logs are involved.

Security problems (for example a way around the team keys, budgets or rate limits) shouldn't go in a public issue; see [SECURITY.md](SECURITY.md).

By taking part you agree to follow the [code of conduct](CODE_OF_CONDUCT.md).
