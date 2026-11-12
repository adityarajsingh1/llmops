# Configuration and features

Everything the gateway does is set in `gateway.yaml` (start from [gateway.example.yaml](../gateway.example.yaml)) and a few `LLMOPS_*` settings. This page goes through it piece by piece.

## The gateway endpoint

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

## Providers

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

## Prompt registry

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

## When a provider fails

Every call to a provider gets a timeout and is retried with exponential backoff (with jitter, so a fleet of gateways doesn't retry in lockstep) when the error is worth retrying: 429s, 5xx, timeouts and network errors. A bad request or a bad key isn't retried.

If a model still fails, the gateway tries its `fallbacks` in order, so a request for `gpt-5-mini` can be answered by `claude-haiku` during an OpenAI outage. The response's `model` field says which model actually answered. A request the provider rejected as invalid (400, 413, 422) isn't sent to the fallbacks, since they would reject it too.

Each provider has a circuit breaker. After 5 failures in a row it opens and the gateway stops calling that provider for 30 seconds, so requests go straight to the fallback instead of waiting on timeouts. Then one trial request is let through, and a success closes the breaker again. All of these numbers are under `reliability:` in `gateway.yaml`.

## Costs and budgets

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

## Caching

Plenty of LLM traffic repeats itself: the same FAQ question, the same ticket classified again after the app retried. With `cache: {enabled: true}` in `gateway.yaml`, an identical request is answered from memory, at no cost, with `x-llmops-cache: hit` on the response. Cache hits aren't recorded as usage and don't count against budgets.

Only requests with `temperature: 0` are cached unless `cache_nondeterministic` is on, because a caller who asked for a higher temperature wants different answers. Entries are kept per team unless `shared_across_teams` is on, so a team never gets an answer generated from another team's prompt. A model can opt out with `cache: false` on its entry. The cache lives in the gateway process; a Redis backend (implement `CacheBackend` in `cache.py`) would let several gateways share one.

The **semantic cache** (`cache.semantic.enabled`) also reuses an answer when a new question is close to an earlier one, and marks the response `x-llmops-cache: semantic-hit`. Everything except the last user message has to match exactly (same system prompt, same earlier turns, same settings). Only the last message is compared, by cosine similarity of embeddings, against `threshold` (0.9 by default). The embedder is pluggable:

- `hashing` (the default) is built in and needs no network. It hashes words and character trigrams, so it matches rewordings that use the same words ("How do I reset my password?" and "Reset my password, how do I do it?" score 0.93). It doesn't understand meaning, so paraphrases with different words won't match, and it keeps "...on mobile?" (0.86) apart from the original at the default threshold.
- `openai` uses an embedding model and matches real paraphrases.
- Others plug in through the `llmops.embedders` entry point group.

A wrong semantic hit means a wrong answer, so start with a high threshold and watch `llmops_cache_lookups_total{result="semantic_hit"}` before lowering it. If the embedder fails, the gateway logs it and carries on without the semantic cache.

## Rate limits

A team can get `rate_limit: {requests_per_minute: 120, burst: 20}`. It's a token bucket, so up to `burst` requests can go through back to back and the allowance refills evenly over the minute; one runaway loop in an app gets slowed down without punishing normal bursty traffic. Over the limit, requests get a 429 `rate_limit_exceeded` with a `Retry-After` header, and every response carries `x-ratelimit-limit-requests` and `x-ratelimit-remaining-requests` like OpenAI's API, so SDKs that back off on 429s just work. Limits are kept per gateway process. A tokens-per-minute limit would be a good addition.

## Settings

Settings come from `LLMOPS_*` environment variables or a `.env` file (see `.env.example`). Bad values (a port of 70000, an unknown environment) fail at startup with a clear message instead of later.
