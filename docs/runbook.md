# Runbook

What to do when an llmops alert fires. Each section matches an alert in `deploy/prometheus/alerts.yml`. The Grafana dashboard and the gateway's JSON logs are the first places to look; every log line has a `request_id`, and so does every response (`x-request-id`), so a single bad request can be followed from the app to the provider.

Useful commands throughout:

```bash
curl -s localhost:8000/health                       # is it up, which version, which instance
curl -s localhost:8000/metrics | grep llmops_circuit_state
docker compose logs gateway --since 15m | jq 'select(.level != "info")'
llmops usage --month 2026-11                         # spend so far, per team and model
```

## Gateway down

`LLMOpsGatewayDown`: Prometheus can't scrape `/metrics`.

1. Is the process running? `docker compose ps` or your orchestrator's view. The container has a health check on `/health`.
2. If it keeps restarting, read the last logs. The usual cause is a config error at startup: a broken `gateway.yaml` or a bad `LLMOPS_*` setting fails loudly with the file name and the field, by design.
3. Check the config without starting the server: `python -c "from llmops.config import GatewayConfig; GatewayConfig.load('gateway.yaml')"` and `llmops prompts check`.
4. If it runs but Prometheus still can't reach it, check `LLMOPS_METRICS_ENABLED` and the network between them.

Apps see connection errors while the gateway is down. There's no gateway-side fix for that besides running more than one instance behind a load balancer.

## High error rate

`LLMOpsHighErrorRate`: more than 5% of requests end in 502 or 503 for 10 minutes.

1. Dashboard, "Requests by status" and "Provider errors": is it one provider or all of them?
2. **One provider:** check its status page. 503 (`provider_unavailable`) means retries and fallbacks ran out; 502 (`provider_error`) means the provider rejected the request outright, often a bad or expired API key (`401` in the error message) or a model name it no longer knows.
3. **A model with no fallbacks** fails as soon as its provider does. Adding `fallbacks: [...]` in `gateway.yaml` is the fix for next time.
4. **All providers:** suspect the gateway's own network (DNS, egress proxy, firewall). Errors that say "timed out" or "connection" point there.
5. Grab a `request_id` from a failing response or log line and follow it through the logs and the trace.

## Circuit open

`LLMOpsCircuitOpen` (a provider's breaker has been open for 5 minutes) and `LLMOpsManyFallbacks` (more than 10% of a model's requests answered by a fallback).

An open breaker is the gateway protecting itself: after 5 failures in a row, it stops calling that provider for 30 seconds and sends requests straight to the fallbacks, then lets one trial request through. Staying open means the trial requests keep failing.

1. Check the provider's status page and the "Provider errors" panel for the error type.
2. If the provider is fine but the breaker isn't, look at the errors in the logs. A timeout that's too short for a slow model (`reliability.retry.timeout`) can look like an outage.
3. While it's open, traffic runs on the fallback models, which may cost differently and answer differently. Keep an eye on the cost panel and the monitors.
4. Nothing needs resetting by hand: the breaker closes by itself on the first successful trial.

## Slow responses

`LLMOpsSlowResponses`: p95 latency for a model above 20 seconds for 15 minutes.

1. Is it the provider or the gateway? Compare `llmops_provider_duration_seconds` (time spent at the provider, all tries together) with `llmops_request_duration_seconds` (what the app waits). If they're close, it's the provider.
2. Retries add up: three tries with a 30s timeout can take well over a minute. If the provider is slow but working, a lower `timeout` with fallbacks gives apps a faster answer.
3. Long answers are slow answers. Check whether `max_tokens` went up in a prompt, or whether the weekly report shows answers getting longer.

## Requests refused

`LLMOpsRequestsRefused`: more than 10% of a team's requests get a 429.

The error `code` in the response body says which limit it is:

- `budget_exceeded`: the team spent its monthly budget. Check with `llmops usage --team <team>`. Either the budget is too small (raise `budget.monthly_usd`, or switch to `on_exceed: warn` for now) or something is burning money, such as a loop in an app or a prompt that suddenly got much longer. The weekly report's cost per request shows the second.
- `rate_limit_exceeded`: the team sends more than `rate_limit.requests_per_minute`. Clients get a `Retry-After` header; the OpenAI SDKs back off on 429s by themselves. A sudden burst usually means a retry loop in an app.

## Monitor failing

`LLMOpsMonitorFailing`: a live quality monitor fails on more than 20% of sampled answers (with at least 10 samples in the hour).

1. Did a prompt change recently? `git log prompts/` and `llmops prompts diff <name> <old> <new>`. Rolling back is a one-line change to `active:`.
2. Did the provider change the model? It happens without notice under the same name. If nobody touched the prompt and the failures started on a specific day, that's the likely cause; running the eval suite (`llmops eval run evals/<prompt>.yaml`) confirms it. Pinning a dated model version in `upstream:` avoids it.
3. Look at real failing answers: they're in the `samples` table of the usage database, with which monitor failed:
   ```bash
   sqlite3 llmops-usage.db <<'SQL'
   SELECT ts, model, prompt, substr(output, 1, 200) FROM samples
   WHERE json_extract(checks, '$."<monitor-name>"') = 0 ORDER BY ts DESC LIMIT 20;
   SQL
   ```
4. If the check itself is wrong (it flags good answers), fix the monitor in `gateway.yaml`. Then add the case to the eval suite, so the next prompt change is tested against it.

## After an incident

Run `llmops report` for the week, and write down what happened while it's fresh: the alert, the cause, what fixed it. If an alert fired too late or too early, adjust it in `alerts.yml` and add a case to `alerts.test.yml`, so `promtool test rules` keeps it honest.
