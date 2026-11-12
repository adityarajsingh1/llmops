# Quality: evals and monitoring

Two halves of the same idea: evals check prompt changes before they ship, monitors and the weekly report watch real traffic after.

## Evals: catching prompt changes that make answers worse

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

## Monitoring live answers

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

The monitors also have an alert rule (`LLMOpsMonitorFailing`), see [observability](observability.md#alerts).
