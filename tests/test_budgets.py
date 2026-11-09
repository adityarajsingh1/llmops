from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from test_gateway import SEARCH, chat, no_sleep

from llmops.app import create_app
from llmops.auth import hash_key
from llmops.budgets import Budget, check, month_start
from llmops.config import GatewayConfig
from llmops.gateway import Gateway
from llmops.providers.fake import FakeProvider
from llmops.usage import MemoryUsageStore, UsageRecord


def test_month_start_is_utc():
    berlin = timezone(timedelta(hours=1))
    # 00:30 on Dec 1 in Berlin is still Nov 30 in UTC
    start = month_start(datetime(2026, 12, 1, 0, 30, tzinfo=berlin))
    assert start == datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "spent, exceeded, warns",
    [(0, False, False), (7.9, False, False), (8, False, True), (9.99, False, True), (10, True, True)],
)
def test_check(spent, exceeded, warns):
    status = check(Budget(monthly_usd=10), spent)
    assert status.exceeded is exceeded and (status.warning is not None) is warns
    assert status.remaining_usd == max(0, 10 - spent)


def test_budget_validation():
    with pytest.raises(ValueError):
        Budget(monthly_usd=0)
    with pytest.raises(ValueError):
        Budget(monthly_usd=5, on_exceed="ignore")


def spend(ts, cost):
    return UsageRecord(
        ts=ts,
        team="search",
        model="echo",
        provider="demo",
        prompt_tokens=1,
        completion_tokens=1,
        cost_usd=cost,
    )


def budget_client(settings, spent, **budget):
    data = {
        "providers": {"demo": {"type": "fake"}},
        "models": {"echo": {"provider": "demo", "price": {"input": 1, "output": 1}}},
        "teams": [
            {"name": "search", "key_sha256": hash_key(SEARCH), "budget": {"monthly_usd": 10, **budget}}
        ],
    }
    usage = MemoryUsageStore()
    now = datetime.now(timezone.utc)
    if spent:
        usage.record(spend(now, spent))
    # last month's spend doesn't count against this month
    usage.record(spend(month_start(now) - timedelta(seconds=1), 1000))
    fake = FakeProvider()
    gateway = Gateway(
        GatewayConfig.model_validate(data), providers={"demo": fake}, sleep=no_sleep, usage=usage
    )
    return TestClient(create_app(settings, gateway)), fake


def test_under_budget(settings):
    client, _ = budget_client(settings, spent=2)
    r = chat(client)
    assert r.status_code == 200
    assert float(r.headers["x-llmops-budget-remaining-usd"]) == pytest.approx(8)
    assert "x-llmops-budget-warning" not in r.headers


def test_close_to_the_budget_warns(settings):
    client, _ = budget_client(settings, spent=8.5)
    r = chat(client)
    assert r.status_code == 200 and "85%" in r.headers["x-llmops-budget-warning"]


def test_over_budget_blocks_without_calling_the_provider(settings):
    client, fake = budget_client(settings, spent=10)
    r = chat(client)
    assert r.status_code == 429
    error = r.json()["error"]
    assert error["code"] == "budget_exceeded" and error["type"] == "insufficient_quota"
    assert "$10.00" in error["message"]
    assert fake.requests == []


def test_over_budget_in_warn_mode_goes_through(settings):
    client, fake = budget_client(settings, spent=12, on_exceed="warn")
    r = chat(client)
    assert r.status_code == 200 and "used up" in r.headers["x-llmops-budget-warning"]
    assert r.headers["x-llmops-budget-remaining-usd"] == "0.0000"


def test_teams_without_a_budget_get_no_budget_headers(settings):
    from test_gateway import make_client

    client, _ = make_client(settings)
    r = chat(client)
    assert r.status_code == 200 and "x-llmops-budget-remaining-usd" not in r.headers


def test_month_range():
    from llmops.budgets import month_range

    assert month_range("2026-12") == (
        datetime(2026, 12, 1, tzinfo=timezone.utc),
        datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(ValueError, match="2026-11"):
        month_range("2026-13")


def usage_for(client, key=SEARCH, **params):
    return client.get("/v1/usage", params=params, headers={"Authorization": f"Bearer {key}"})


def test_usage_endpoint_shows_this_months_spend(settings):
    client, _ = budget_client(settings, spent=2.5)
    chat(client)
    body = usage_for(client).json()
    assert body["team"] == "search" and body["budget_usd"] == 10
    assert body["spent_usd"] > 2.5 and body["remaining_usd"] == pytest.approx(10 - body["spent_usd"])
    assert [m["model"] for m in body["models"]] == ["echo"] and body["models"][0]["requests"] == 2


def test_usage_endpoint_for_an_earlier_month(settings):
    client, _ = budget_client(settings, spent=0)
    last_month = month_start() - timedelta(days=1)
    body = usage_for(client, month=f"{last_month:%Y-%m}").json()
    assert body["spent_usd"] == 1000 and body["remaining_usd"] == 0


@pytest.mark.parametrize("month", ["2026-13", "november", "2026-1"])
def test_usage_endpoint_rejects_bad_months(settings, month):
    client, _ = budget_client(settings, spent=0)
    assert usage_for(client, month=month).status_code == 400


def test_usage_endpoint_needs_a_key(settings):
    client, _ = budget_client(settings, spent=0)
    assert usage_for(client, key="wrong").status_code == 401
