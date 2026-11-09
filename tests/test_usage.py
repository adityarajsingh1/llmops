from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from test_gateway import SEARCH, chat, no_sleep

from llmops.app import create_app
from llmops.auth import hash_key
from llmops.config import GatewayConfig
from llmops.gateway import Gateway
from llmops.providers import Usage
from llmops.providers.fake import FakeProvider, failing
from llmops.usage import MemoryUsageStore, Price, SQLiteUsageStore, UsageRecord, open_store

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_price_is_per_million_tokens():
    price = Price(input=2.5, output=10)
    assert price.cost(Usage(prompt_tokens=1_000_000, completion_tokens=0)) == 2.5
    assert price.cost(Usage(prompt_tokens=2000, completion_tokens=500)) == pytest.approx(0.01)


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    s = MemoryUsageStore() if request.param == "memory" else SQLiteUsageStore(tmp_path / "usage.db")
    yield s
    s.close()


def rec(team, model, hours, cost, tokens=(10, 5)):
    return UsageRecord(
        ts=T0 + timedelta(hours=hours),
        team=team,
        model=model,
        provider="p",
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
        cost_usd=cost,
    )


def test_summary_groups_by_team_and_model(store):
    records = [
        rec("b", "m1", 1, 0.5),
        rec("a", "m2", 2, 1.0),
        rec("a", "m1", 3, 0.25),
        rec("a", "m1", 4, 0.25),
    ]
    for r in records:
        store.record(r)
    rows = store.summary(T0)
    assert [(r.team, r.model, r.requests) for r in rows] == [("a", "m1", 2), ("a", "m2", 1), ("b", "m1", 1)]
    assert rows[0].prompt_tokens == 20 and rows[0].completion_tokens == 10
    assert rows[0].cost_usd == pytest.approx(0.5)


def test_time_window_and_team_filter(store):
    for h in (0, 5, 10):
        store.record(rec("a", "m", h, 1.0))
    store.record(rec("b", "m", 5, 7.0))
    assert store.spend("a", T0 + timedelta(hours=5)) == pytest.approx(2.0)
    rows = store.summary(T0 + timedelta(hours=1), until=T0 + timedelta(hours=10))
    assert [(r.team, r.requests) for r in rows] == [("a", 1), ("b", 1)]
    assert store.spend("nobody", T0) == 0


def test_sqlite_keeps_records_across_restarts(tmp_path):
    path = tmp_path / "usage.db"
    s = SQLiteUsageStore(path)
    s.record(rec("a", "m", 1, 0.1))
    s.close()
    again = open_store(str(path))
    assert again.spend("a", T0) == pytest.approx(0.1)


def test_sqlite_compares_times_in_utc(tmp_path):
    s = SQLiteUsageStore(tmp_path / "usage.db")
    berlin = timezone(timedelta(hours=1))
    s.record(rec("a", "m", 1, 1.0))  # 01:00 UTC
    # 01:30 in Berlin is 00:30 UTC, so the record is after it
    assert s.spend("a", datetime(2026, 11, 1, 1, 30, tzinfo=berlin)) == 1.0
    with pytest.raises(ValueError, match="timezone"):
        s.spend("a", datetime(2026, 11, 1))


def priced_client(settings, primary):
    data = {
        "providers": {"main": {"type": "fake"}, "backup": {"type": "fake"}},
        "models": {
            "smart": {"provider": "main", "price": {"input": 3, "output": 15}, "fallbacks": ["free"]},
            "free": {"provider": "backup"},
        },
        "teams": [{"name": "search", "key_sha256": hash_key(SEARCH)}],
        "reliability": {"retry": {"attempts": 1}},
    }
    usage = MemoryUsageStore()
    gateway = Gateway(
        GatewayConfig.model_validate(data),
        providers={"main": primary, "backup": FakeProvider()},
        sleep=no_sleep,
        usage=usage,
    )
    return TestClient(create_app(settings, gateway)), usage


def test_each_answer_is_recorded_with_its_cost(settings):
    client, usage = priced_client(settings, FakeProvider())
    r = chat(client, model="smart", headers={"x-request-id": "req-42"})
    assert r.status_code == 200
    [record] = usage.records
    body = r.json()["usage"]
    expected = (body["prompt_tokens"] * 3 + body["completion_tokens"] * 15) / 1e6
    assert record.team == "search" and record.model == "smart" and record.provider == "main"
    assert record.cost_usd == pytest.approx(expected) and record.request_id == "req-42"
    assert float(r.headers["x-llmops-cost-usd"]) == pytest.approx(expected, abs=1e-6)


def test_a_fallback_is_priced_as_the_model_that_answered(settings):
    client, usage = priced_client(settings, FakeProvider(failing(503)))
    assert chat(client, model="smart").json()["model"] == "free"
    assert [(r.model, r.cost_usd) for r in usage.records] == [("free", 0.0)]


def test_failed_requests_cost_nothing(settings):
    client, usage = priced_client(settings, FakeProvider(failing(400)))
    assert chat(client, model="smart").status_code == 502
    assert usage.records == []
