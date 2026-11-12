from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_gateway import SEARCH, chat, no_sleep

from llmops.app import create_app
from llmops.auth import hash_key
from llmops.config import GatewayConfig
from llmops.gateway import Gateway
from llmops.monitoring import Monitor, MonitoringConfig, Sample, run_monitors
from llmops.prompts import PromptRegistry
from llmops.providers.fake import FakeProvider
from llmops.usage import MemoryUsageStore, SQLiteUsageStore

PROMPTS = PromptRegistry.from_dir(Path(__file__).resolve().parent.parent / "prompts")
NO_PRICES = {"type": "regex", "pattern": r"\$\d", "must_match": False}


def sample(output, model="echo", prompt=None, hours=0):
    return Sample(
        ts=datetime(2026, 11, 2, tzinfo=timezone.utc) + timedelta(hours=hours),
        team="search",
        model=model,
        prompt=prompt,
        output=output,
        completion_tokens=len(output.split()),
    )


def test_monitors_apply_by_prompt_and_model():
    monitors = [
        Monitor(name="prices", prompt="support-reply", checks=[NO_PRICES]),
        Monitor(name="short", model="echo", checks=[{"type": "max_words", "max_words": 5}]),
        Monitor(name="everything", checks=[{"type": "not_contains", "value": "as an AI"}]),
    ]
    s = sample("It costs $40 a month, as you can see here", prompt="support-reply")
    assert run_monitors(monitors, s) == {"prices": False, "short": False, "everything": True}
    assert run_monitors(monitors, sample("ok", model="other")) == {"everything": True}


@pytest.mark.parametrize(
    "monitoring, message",
    [
        ({"sample_rate": 1.5}, "less than or equal"),
        ({"monitors": [{"name": "a", "checks": []}]}, "at least 1"),
        (
            {"monitors": [{"name": "a", "checks": [NO_PRICES]}, {"name": "a", "checks": [NO_PRICES]}]},
            "unique",
        ),
        ({"monitors": [{"name": "a", "model": "gpt-9", "checks": [NO_PRICES]}]}, "unknown model 'gpt-9'"),
    ],
)
def test_bad_monitoring_config(monitoring, message):
    data = {"providers": {"demo": {"type": "fake"}}, "models": {"echo": {"provider": "demo"}}}
    with pytest.raises(ValueError, match=message):
        GatewayConfig.model_validate({**data, "monitoring": monitoring})


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_samples_round_trip(store_kind, tmp_path):
    store = MemoryUsageStore() if store_kind == "memory" else SQLiteUsageStore(tmp_path / "u.db")
    a, b = sample("first", hours=1), sample("second", prompt="support-reply", hours=5)
    a.checks = {"prices": True}
    store.record_sample(b)
    store.record_sample(a)
    got = store.samples(datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert [s.output for s in got] == ["first", "second"]
    assert got[0].checks == {"prices": True} and got[1].prompt == "support-reply"
    assert store.samples(a.ts + timedelta(hours=1)) == [got[1]]


def monitored_client(settings, replies, sample_rate=1.0, draws=None):
    data = {
        "providers": {"demo": {"type": "fake"}},
        "models": {"gpt-5-mini": {"provider": "demo"}, "echo": {"provider": "demo"}},
        "teams": [{"name": "search", "key_sha256": hash_key(SEARCH)}],
        "monitoring": {
            "sample_rate": sample_rate,
            "monitors": [{"name": "no-prices", "prompt": "support-reply", "checks": [NO_PRICES]}],
        },
    }
    draws = iter(draws or [0.0] * 100)
    usage = MemoryUsageStore()
    gateway = Gateway(
        GatewayConfig.model_validate(data),
        PROMPTS,
        providers={"demo": FakeProvider(replies)},
        sleep=no_sleep,
        usage=usage,
        sampler=lambda: next(draws),
    )
    return TestClient(create_app(settings, gateway)), usage


VARIABLES = {"product": "Acme", "article": "See the pricing page.", "question": "How much is it?"}


def ask(client):
    return chat(client, model=None, messages=None, prompt={"name": "support-reply", "variables": VARIABLES})


def test_sampled_answers_are_checked_and_counted(settings):
    from test_metrics import scrape, value

    client, usage = monitored_client(settings, ["It's $49 a month.", "See the pricing page."])
    ask(client)
    ask(client)
    assert [(s.prompt, s.prompt_version, s.checks) for s in usage.sampled] == [
        ("support-reply", 2, {"no-prices": False}),
        ("support-reply", 2, {"no-prices": True}),
    ]
    m = scrape(client)
    assert value(m, "llmops_monitor_checks_total", monitor="no-prices", result="fail") == 1
    assert value(m, "llmops_monitor_checks_total", monitor="no-prices", result="pass") == 1


def test_sampling_rate_is_respected(settings):
    client, usage = monitored_client(settings, ["a", "b", "c"], sample_rate=0.5, draws=[0.1, 0.7, 0.49])
    for _ in range(3):
        chat(client)
    assert [s.output for s in usage.sampled] == ["a", "c"]
    # plain messages have no prompt, so the support-reply monitor doesn't apply
    assert usage.sampled[0].prompt is None and usage.sampled[0].checks == {}


def test_off_by_default(settings):
    client, usage = monitored_client(settings, ["a"], sample_rate=0.0)
    chat(client)
    assert usage.sampled == [] and len(usage.records) == 1


def test_example_config_monitoring_section_is_valid():
    MonitoringConfig()  # defaults
    root = Path(__file__).resolve().parent.parent
    GatewayConfig.load(root / "gateway.example.yaml")
