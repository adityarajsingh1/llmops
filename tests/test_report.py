from datetime import datetime, timedelta, timezone

import pytest

from llmops import cli
from llmops.monitoring import Sample
from llmops.report import to_markdown, weekly_report
from llmops.usage import MemoryUsageStore, SQLiteUsageStore, UsageRecord

NOW = datetime(2026, 11, 16, 9, tzinfo=timezone.utc)


def usage(store, days_ago, team, model, cost, completion_tokens=100, n=1):
    for _ in range(n):
        store.record(
            UsageRecord(
                ts=NOW - timedelta(days=days_ago),
                team=team,
                model=model,
                provider="p",
                prompt_tokens=50,
                completion_tokens=completion_tokens,
                cost_usd=cost,
            )
        )


def samples(store, days_ago, monitor, passed, failed, finish="stop"):
    for ok in [True] * passed + [False] * failed:
        store.record_sample(
            Sample(
                ts=NOW - timedelta(days=days_ago),
                team="search",
                model="m",
                output="x",
                completion_tokens=1,
                finish_reason=finish,
                checks={monitor: ok},
            )
        )


def test_steady_week_has_no_drift():
    store = MemoryUsageStore()
    usage(store, 10, "search", "gpt-5-mini", 0.01, n=10)
    usage(store, 3, "search", "gpt-5-mini", 0.011, n=12)
    report = weekly_report(store, now=NOW)
    assert report.drift == []
    [team] = report.teams
    assert team.requests == 12 and team.cost_usd == pytest.approx(0.132)
    assert team.previous_cost_usd == pytest.approx(0.1)
    assert "Nothing drifted" in to_markdown(report)


def test_cost_and_length_drift_are_flagged():
    store = MemoryUsageStore()
    usage(store, 9, "search", "gpt-5-mini", 0.01, completion_tokens=100, n=5)
    # a provider-side change: same traffic, answers twice as long and expensive
    usage(store, 2, "search", "gpt-5-mini", 0.02, completion_tokens=210, n=5)
    report = weekly_report(store, now=NOW)
    assert report.drift == [
        "gpt-5-mini: cost per request changed by more than 25%",
        "gpt-5-mini: answer length changed by more than 25%",
    ]
    [model] = report.models
    assert model.cost_per_request == pytest.approx(0.02) and model.previous_cost_per_request == pytest.approx(
        0.01
    )


def test_old_and_future_records_are_ignored():
    store = MemoryUsageStore()
    usage(store, 20, "search", "m", 5)
    usage(store, -1, "search", "m", 5)
    report = weekly_report(store, now=NOW)
    assert report.teams == [] and report.models == []


def test_monitor_drops_need_enough_samples():
    store = MemoryUsageStore()
    samples(store, 10, "prices", passed=30, failed=0)
    samples(store, 2, "prices", passed=24, failed=6)  # 100% -> 80%
    samples(store, 10, "rare", passed=3, failed=0)
    samples(store, 2, "rare", passed=0, failed=3)  # too few to say anything
    report = weekly_report(store, now=NOW)
    assert report.drift == ["monitor prices: pass rate dropped by more than 5% points"]
    rare = next(m for m in report.monitors if m.monitor == "rare")
    assert rare.pass_rate == 0 and rare.previous_pass_rate == 1
    assert "| prices | 30 | 80.0% | 100.0% (30) |" in to_markdown(report)


def test_more_truncated_answers_are_flagged():
    store = MemoryUsageStore()
    samples(store, 10, "m", passed=10, failed=0)
    samples(store, 2, "m", passed=8, failed=0)
    samples(store, 2, "m", passed=2, failed=0, finish="length")
    report = weekly_report(store, now=NOW)
    assert report.truncated_share == pytest.approx(0.2) and report.previous_truncated_share == 0
    assert "more answers are being cut off at max_tokens" in report.drift


def test_report_cli(tmp_path, capsys):
    db = tmp_path / "u.db"
    store = SQLiteUsageStore(db)
    now = datetime.now(timezone.utc)
    for days, cost in [(9, 0.01), (2, 0.05)]:
        store.record(
            UsageRecord(
                ts=now - timedelta(days=days), team="search", model="m", provider="p",
                prompt_tokens=1, completion_tokens=10, cost_usd=cost,
            )
        )  # fmt: skip
    store.close()

    with pytest.raises(SystemExit) as code:
        cli.main(["report", "--db", str(db)])
    out = capsys.readouterr().out
    assert code.value.code == 0 and "## Needs a look" in out and "| search | 1 | 0.05 | 0.01 |" in out

    with pytest.raises(SystemExit) as code:
        cli.main(["report", "--db", str(db), "--fail-on-drift", "--json"])
    assert code.value.code == 2 and '"drift"' in capsys.readouterr().out

    with pytest.raises(SystemExit) as code:
        cli.main(["report", "--db", str(tmp_path / "missing.db")])
    assert code.value.code == 1
