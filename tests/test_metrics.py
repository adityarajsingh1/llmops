import pytest
from prometheus_client.parser import text_string_to_metric_families
from test_gateway import OPS, SEARCH, chat, fallback_client, make_client

from llmops.providers.fake import FakeProvider, failing


def scrape(client) -> dict[tuple[str, tuple], float]:
    """/metrics as {(sample name, sorted labels): value}."""
    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    samples = {}
    for family in text_string_to_metric_families(r.text):
        for s in family.samples:
            samples[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return samples


def value(samples, name, **labels):
    return samples.get((name, tuple(sorted(labels.items()))), 0.0)


def test_requests_tokens_and_latency_are_counted(settings):
    client, _ = make_client(settings)
    r1, r2 = chat(client), chat(client, model="gpt-5-mini")
    m = scrape(client)
    assert value(m, "llmops_requests_total", team="search", model="echo", status="200") == 1
    assert value(m, "llmops_requests_total", team="search", model="gpt-5-mini", status="200") == 1
    assert (
        value(m, "llmops_tokens_total", team="search", model="echo", kind="prompt")
        == (r1.json()["usage"]["prompt_tokens"])
    )
    assert (
        value(m, "llmops_tokens_total", team="search", model="gpt-5-mini", kind="completion")
        == (r2.json()["usage"]["completion_tokens"])
    )
    assert value(m, "llmops_request_duration_seconds_count", model="echo") == 1
    assert value(m, "llmops_request_duration_seconds_bucket", model="echo", le="+Inf") == 1


def test_errors_are_counted_by_status(settings):
    client, _ = make_client(settings, fake=FakeProvider(failing(500, 3)))
    chat(client)  # 503 after retries
    chat(client, model="gpt-big")  # 403, not allowed for this team
    chat(client, key="wrong")  # 401
    m = scrape(client)
    assert value(m, "llmops_requests_total", team="search", model="echo", status="503") == 1
    assert value(m, "llmops_requests_total", team="search", model="gpt-big", status="403") == 1
    assert value(m, "llmops_requests_total", team="unknown", model="echo", status="401") == 1


def test_made_up_model_names_dont_create_series(settings):
    client, _ = make_client(settings)
    for i in range(3):
        chat(client, key=OPS, model=f"random-{i}")
    m = scrape(client)
    assert value(m, "llmops_requests_total", team="ops", model="unknown", status="404") == 3
    assert not any("random-" in str(labels) for _, labels in m)


def test_cost_is_counted_for_the_model_that_answered(settings):
    client, _ = fallback_client(settings, FakeProvider(failing(503, 5)), FakeProvider(["ok"]))
    chat(client, model="smart")
    m = scrape(client)
    assert value(m, "llmops_requests_total", team="search", model="smart", status="200") == 1
    assert value(m, "llmops_tokens_total", team="search", model="cheap", kind="completion") > 0
    assert value(m, "llmops_cost_usd_total", team="search", model="cheap") == 0


def test_metrics_can_be_turned_off(settings):
    client, _ = make_client(settings.model_copy(update={"metrics_enabled": False}))
    assert client.get("/metrics").status_code == 404


@pytest.mark.parametrize("path", ["/health", "/v1/models"])
def test_other_endpoints_dont_count_as_chat_requests(settings, path):
    client, _ = make_client(settings)
    client.get(path, headers={"Authorization": f"Bearer {SEARCH}"})
    assert not any(name == "llmops_requests_total" for name, _ in scrape(client))


def test_hidden_retries_and_fallbacks_show_up(settings):
    # main fails twice (attempts: 2 in fallback_client), which opens its breaker; backup answers
    client, _ = fallback_client(settings, FakeProvider(failing(503, 5)), FakeProvider(["ok", "ok"]))
    chat(client, model="smart")
    m = scrape(client)
    assert value(m, "llmops_provider_errors_total", provider="main", retryable="true") == 2
    assert value(m, "llmops_fallbacks_total", model="smart", fallback="cheap") == 1
    assert value(m, "llmops_circuit_state", provider="main") == 2
    assert value(m, "llmops_circuit_state", provider="backup") == 0
    assert value(m, "llmops_provider_duration_seconds_count", provider="main", model="smart") == 1
    assert value(m, "llmops_provider_duration_seconds_count", provider="backup", model="cheap") == 1


def test_a_retry_that_succeeds_still_counts_the_error(settings):
    client, _ = make_client(settings, fake=FakeProvider([*failing(429), "fine"]))
    assert chat(client).status_code == 200
    m = scrape(client)
    assert value(m, "llmops_provider_errors_total", provider="demo", retryable="true") == 1
    assert value(m, "llmops_requests_total", team="search", model="echo", status="200") == 1
