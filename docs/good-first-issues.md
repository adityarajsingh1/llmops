# Good first issues

Well-scoped things that would make llmops better. Each one names where to start and how to know it's done. If you pick one, open an issue first saying you're on it, so two people don't build the same thing. Bigger items are further down.

## Small

**A `starts_with` / `ends_with` check.** Some prompts must open with a greeting or end with a sign-off. Add a scorer in `llmops/evals/scorers.py` next to `Contains`, with `ignore_case` like the others, and a row in the parametrized `test_scorers` in `tests/test_evals.py`.

**A `min_words` check.** The mirror of `max_words`, for answers that come back suspiciously short. Same place as above.

**Show prompt variables in `llmops prompts list`.** Today `list` shows versions; adding the active version's variables would save a `show` call. `prompts_command` in `llmops/cli.py`, with a test in `tests/test_cli.py`.

**`llmops keys hash`.** Print the SHA-256 of an existing key (read from stdin, so it doesn't end up in shell history), for teams migrating keys they already have. `llmops/cli.py` and `llmops/auth.py`.

**A `LLMOpsBudgetNearlyUsed` alert.** Budgets are only visible through headers and `/v1/usage` today. Export a gauge with each team's spend as a share of its budget (`metrics.py`, updated in `check_budget`), add an alert at 90% to `deploy/prometheus/alerts.yml` with a test case in `alerts.test.yml`, and a runbook section.

**A Grafana panel for monitors.** The dashboard has no panel for `llmops_monitor_checks_total` yet. Add a pass-rate-per-monitor panel to `deploy/grafana/dashboards/llmops.json`.

## Medium

**Tokens-per-minute rate limits.** `rate_limit` only counts requests. Tokens per minute matter more for cost and for provider quotas. The tricky part is that the token count is only known after the answer, so it needs the same "check before, count after" approach as budgets. `llmops/ratelimit.py`, `Gateway.check_rate_limit`.

**A Redis cache backend.** The cache lives in one process, so several gateways behind a load balancer each have their own. Implement `CacheBackend` (`llmops/cache.py`) on Redis, as an optional extra (`pip install llmops[redis]`), with tests that use `fakeredis`.

**A Postgres usage store.** Same reason: several gateways need one usage database for budgets to be right. Implement `UsageStore` (`llmops/usage.py`); the existing store tests are parametrized, so add it to the fixture and run them against a Postgres service in CI.

**An LLM-as-judge scorer.** The built-in checks are deterministic on purpose, but some things (tone, "did it actually answer the question") need a model. A `judge` scorer that sends the answer and a rubric to a model through the gateway, with a cache so the same answer isn't judged twice. Needs some thought on how a scorer gets access to a provider; open an issue to discuss the design first.

**Mistral, Gemini or Bedrock providers.** One class each in `llmops/providers/`, following `anthropic.py`, tested with `httpx.MockTransport`. Could also live in a separate package through the `llmops.providers` entry point.

## Bigger

**Streaming.** `stream: true` returns a 400 today. Supporting it touches the provider interface (an async iterator of chunks), the endpoint (server-sent events in OpenAI's format), usage accounting (tokens arrive with the last chunk), caching and retries (you can't retry halfway through a stream). Worth a design issue before any code.

**Admin API.** Teams, keys and budgets live in `gateway.yaml` and need a restart to change. An authenticated admin API with hot reload would make the gateway much easier to run.
