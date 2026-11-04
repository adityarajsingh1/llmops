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

The gateway endpoint that ties these together is next.

```bash
pip install -e ".[dev]"
llmops serve            # http://127.0.0.1:8000/health, API docs at /docs
```

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
