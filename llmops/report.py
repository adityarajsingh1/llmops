"""
The weekly report: this week against the week before, from the usage
database. It answers the questions that come up every Monday: what did each
team spend, did anything get more expensive per request, are answers getting
longer or cut off more often, and are the live monitors passing less.

Changes bigger than the thresholds are flagged as drift. The thresholds are
deliberately simple and a starting point, not statistics; small teams with a
handful of requests will see noisy numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from llmops.monitoring import Sample
from llmops.usage import UsageRow, UsageStore

# relative change in cost per request or tokens per answer that counts as drift
RELATIVE_DRIFT = 0.25
# drop in a monitor's pass rate, in percentage points
PASS_RATE_DROP = 0.05
# monitors need at least this many samples in both weeks before they're compared
MIN_SAMPLES = 20


class Period(BaseModel):
    start: datetime
    end: datetime


class TeamLine(BaseModel):
    team: str
    requests: int
    cost_usd: float
    previous_cost_usd: float


class ModelLine(BaseModel):
    model: str
    requests: int
    cost_per_request: float | None
    previous_cost_per_request: float | None
    completion_tokens_per_request: float | None
    previous_completion_tokens_per_request: float | None


class MonitorLine(BaseModel):
    monitor: str
    samples: int
    pass_rate: float | None
    previous_samples: int
    previous_pass_rate: float | None


class WeeklyReport(BaseModel):
    current: Period
    previous: Period
    teams: list[TeamLine]
    models: list[ModelLine]
    monitors: list[MonitorLine]
    truncated_share: float | None
    previous_truncated_share: float | None
    drift: list[str]


def _per_model(rows: list[UsageRow]) -> dict[str, tuple[int, float, int]]:
    out: dict[str, tuple[int, float, int]] = {}
    for r in rows:
        n, cost, tokens = out.get(r.model, (0, 0.0, 0))
        out[r.model] = (n + r.requests, cost + r.cost_usd, tokens + r.completion_tokens)
    return out


def _monitor_rates(samples: list[Sample]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for s in samples:
        for name, passed in s.checks.items():
            total, ok = out.get(name, (0, 0))
            out[name] = (total + 1, ok + passed)
    return out


def _truncated(samples: list[Sample]) -> float | None:
    # finish_reason "length" means the answer hit max_tokens and was cut off
    if not samples:
        return None
    return sum(s.finish_reason == "length" for s in samples) / len(samples)


def _ratio(a: float, b: int) -> float | None:
    return a / b if b else None


def _changed(now: float | None, before: float | None) -> bool:
    return (
        now is not None and before is not None and before > 0 and abs(now - before) / before > RELATIVE_DRIFT
    )


def weekly_report(store: UsageStore, now: datetime | None = None) -> WeeklyReport:
    end = now or datetime.now(timezone.utc)
    current = Period(start=end - timedelta(days=7), end=end)
    previous = Period(start=end - timedelta(days=14), end=current.start)

    rows_now = store.summary(current.start, current.end)
    rows_before = store.summary(previous.start, previous.end)
    samples_now = store.samples(current.start, current.end)
    samples_before = store.samples(previous.start, previous.end)
    drift: list[str] = []

    spend_before: dict[str, float] = {}
    for r in rows_before:
        spend_before[r.team] = spend_before.get(r.team, 0.0) + r.cost_usd
    teams: dict[str, TeamLine] = {}
    for r in rows_now:
        team_line = teams.setdefault(
            r.team,
            TeamLine(team=r.team, requests=0, cost_usd=0, previous_cost_usd=spend_before.get(r.team, 0)),
        )
        team_line.requests += r.requests
        team_line.cost_usd += r.cost_usd

    models = []
    by_model_now, by_model_before = _per_model(rows_now), _per_model(rows_before)
    for model in sorted(by_model_now):
        n, cost, tokens = by_model_now[model]
        pn, pcost, ptokens = by_model_before.get(model, (0, 0.0, 0))
        model_line = ModelLine(
            model=model,
            requests=n,
            cost_per_request=_ratio(cost, n),
            previous_cost_per_request=_ratio(pcost, pn),
            completion_tokens_per_request=_ratio(tokens, n),
            previous_completion_tokens_per_request=_ratio(ptokens, pn),
        )
        models.append(model_line)
        if _changed(model_line.cost_per_request, model_line.previous_cost_per_request):
            drift.append(f"{model}: cost per request changed by more than {RELATIVE_DRIFT:.0%}")
        if _changed(
            model_line.completion_tokens_per_request, model_line.previous_completion_tokens_per_request
        ):
            drift.append(f"{model}: answer length changed by more than {RELATIVE_DRIFT:.0%}")

    monitors = []
    rates_now, rates_before = _monitor_rates(samples_now), _monitor_rates(samples_before)
    for name in sorted(rates_now.keys() | rates_before.keys()):
        total, ok = rates_now.get(name, (0, 0))
        ptotal, pok = rates_before.get(name, (0, 0))
        line = MonitorLine(
            monitor=name,
            samples=total,
            pass_rate=_ratio(ok, total),
            previous_samples=ptotal,
            previous_pass_rate=_ratio(pok, ptotal),
        )
        monitors.append(line)
        enough = total >= MIN_SAMPLES and ptotal >= MIN_SAMPLES
        now_rate, before_rate = line.pass_rate, line.previous_pass_rate
        if (
            enough
            and now_rate is not None
            and before_rate is not None
            and before_rate - now_rate > PASS_RATE_DROP
        ):
            drift.append(f"monitor {name}: pass rate dropped by more than {PASS_RATE_DROP:.0%} points")

    truncated, previous_truncated = _truncated(samples_now), _truncated(samples_before)
    if (
        truncated is not None
        and previous_truncated is not None
        and truncated - previous_truncated > PASS_RATE_DROP
    ):
        drift.append("more answers are being cut off at max_tokens")

    return WeeklyReport(
        current=current,
        previous=previous,
        teams=sorted(teams.values(), key=lambda t: t.cost_usd, reverse=True),
        models=models,
        monitors=monitors,
        truncated_share=truncated,
        previous_truncated_share=previous_truncated,
        drift=drift,
    )


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.1%}"


def _num(x: float | None, digits: int = 1) -> str:
    return "-" if x is None else f"{x:.{digits}f}"


def to_markdown(r: WeeklyReport) -> str:
    out = [f"# llmops weekly report, {r.current.start:%Y-%m-%d} to {r.current.end:%Y-%m-%d}", ""]
    if r.drift:
        out += ["## Needs a look", ""] + [f"- {d}" for d in r.drift] + [""]
    else:
        out += ["Nothing drifted past the thresholds this week.", ""]
    out += ["## Spend by team", "", "| team | requests | cost USD | week before |", "|---|---|---|---|"]
    out += [f"| {t.team} | {t.requests} | {t.cost_usd:.2f} | {t.previous_cost_usd:.2f} |" for t in r.teams]
    out += [
        "",
        "## Models",
        "",
        "| model | requests | USD per request | week before | tokens per answer | week before |",
        "|---|---|---|---|---|---|",
    ]
    out += [
        f"| {m.model} | {m.requests} | {_num(m.cost_per_request, 5)} "
        f"| {_num(m.previous_cost_per_request, 5)} | {_num(m.completion_tokens_per_request)} "
        f"| {_num(m.previous_completion_tokens_per_request)} |"
        for m in r.models
    ]
    if r.monitors:
        out += ["", "## Monitors", "", "| monitor | samples | pass rate | week before |", "|---|---|---|---|"]
        out += [
            f"| {m.monitor} | {m.samples} | {_pct(m.pass_rate)} | {_pct(m.previous_pass_rate)} "
            f"({m.previous_samples}) |"
            for m in r.monitors
        ]
    if r.truncated_share is not None:
        out += [
            "",
            f"Sampled answers cut off at max_tokens: {_pct(r.truncated_share)} "
            f"(week before: {_pct(r.previous_truncated_share)})",
        ]
    return "\n".join(out) + "\n"
