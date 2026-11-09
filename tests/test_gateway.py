from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llmops.app import create_app
from llmops.auth import hash_key
from llmops.config import GatewayConfig
from llmops.gateway import Gateway
from llmops.prompts import PromptRegistry
from llmops.providers.fake import FakeProvider, failing

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
SEARCH, OPS = "llmops-search-key", "llmops-ops-key"


async def no_sleep(seconds: float) -> None:
    pass


def make_client(settings, fake=None, **config_overrides):
    data = {
        "providers": {
            "demo": {"type": "fake"},
            "paid": {"type": "openai", "api_key_env": "NOT_SET_ANYWHERE"},
        },
        "models": {
            "echo": {"provider": "demo"},
            "gpt-5-mini": {"provider": "demo", "upstream": "gpt-5-mini-2025"},
            "gpt-big": {"provider": "paid"},
        },
        "teams": [
            {"name": "search", "key_sha256": hash_key(SEARCH), "models": ["echo", "gpt-5-mini"]},
            {"name": "ops", "key_sha256": hash_key(OPS)},
        ],
    }
    data.update(config_overrides)
    fake = fake or FakeProvider()
    gateway = Gateway(
        GatewayConfig.model_validate(data), PromptRegistry.from_dir(PROMPTS), {"demo": fake}, sleep=no_sleep
    )
    return TestClient(create_app(settings, gateway)), fake


def chat(client, key=SEARCH, headers=None, **body):
    body.setdefault("model", "echo")
    body.setdefault("messages", [{"role": "user", "content": "hello there"}])
    headers = {"Authorization": f"Bearer {key}", **(headers or {})}
    return client.post("/v1/chat/completions", json=body, headers=headers)


def error_code(r):
    return r.json()["error"]["code"]


def test_chat_completion_in_openai_shape(settings):
    client, _ = make_client(settings)
    r = chat(client)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion" and body["id"].startswith("chatcmpl-")
    assert body["model"] == "echo"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "echo: hello there"}
    assert body["choices"][0]["finish_reason"] == "stop"
    usage = body["usage"]
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"] > 0


def test_request_is_translated_for_the_provider(settings):
    client, fake = make_client(settings)
    r = chat(
        client,
        model="gpt-5-mini",
        messages=[{"role": "developer", "content": "be brief"}, {"role": "user", "content": "hi"}],
        max_completion_tokens=50,
        temperature=0.2,
        top_p=0.9,  # unsupported OpenAI fields are ignored, not rejected
    )
    assert r.status_code == 200 and r.json()["model"] == "gpt-5-mini"
    sent = fake.requests[0]
    assert sent.model == "gpt-5-mini-2025"
    assert [m.role for m in sent.messages] == ["system", "user"]
    assert sent.max_tokens == 50 and sent.temperature == 0.2


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": SEARCH}])
def test_bad_or_missing_key(settings, headers):
    client, fake = make_client(settings)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "echo", "messages": [{"role": "user", "content": "x"}]},
        headers=headers,
    )
    assert r.status_code == 401 and error_code(r) == "invalid_api_key"
    assert fake.requests == []


def test_unknown_and_forbidden_models(settings):
    client, _ = make_client(settings)
    r = chat(client, model="nope")
    assert r.status_code == 404 and error_code(r) == "model_not_found"
    r = chat(client, model="gpt-big")
    assert r.status_code == 403 and error_code(r) == "model_not_allowed"


@pytest.mark.parametrize(
    "body",
    [
        {"messages": []},
        {"model": None},
        {"messages": [{"role": "tool", "content": "x"}]},
        {"temperature": 3},
        {"prompt": {"name": "support-reply"}},  # messages and prompt together
    ],
)
def test_invalid_requests_get_openai_style_400s(settings, body):
    client, _ = make_client(settings)
    r = chat(client, **body)
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error" and r.json()["error"]["message"]


def test_streaming_is_rejected_clearly(settings):
    client, _ = make_client(settings)
    r = chat(client, stream=True)
    assert r.status_code == 400 and error_code(r) == "unsupported"


@pytest.mark.parametrize(
    "status, expected, code", [(429, 503, "provider_unavailable"), (400, 502, "provider_error")]
)
def test_provider_errors(settings, status, expected, code):
    # three tries by default, so three failures in a row reach the client
    client, fake = make_client(settings, fake=FakeProvider(failing(status, times=3)))
    r = chat(client)
    assert r.status_code == expected and error_code(r) == code
    assert len(fake.requests) == (3 if status == 429 else 1)


def test_a_retry_hides_a_passing_blip(settings):
    client, fake = make_client(settings, fake=FakeProvider([*failing(503), "fine"]))
    r = chat(client)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "fine"
    assert len(fake.requests) == 2


def fallback_client(settings, primary, backup):
    data = {
        "providers": {"main": {"type": "fake"}, "backup": {"type": "fake"}},
        "models": {
            "smart": {"provider": "main", "upstream": "smart-v1", "fallbacks": ["cheap"]},
            "cheap": {"provider": "backup", "upstream": "cheap-v2"},
        },
        "teams": [{"name": "search", "key_sha256": hash_key(SEARCH), "models": ["smart"]}],
        "reliability": {"retry": {"attempts": 2}, "breaker": {"failure_threshold": 2}},
    }
    gateway = Gateway(
        GatewayConfig.model_validate(data), providers={"main": primary, "backup": backup}, sleep=no_sleep
    )
    return TestClient(create_app(settings, gateway)), gateway


def test_fallback_when_the_provider_is_down(settings):
    primary, backup = FakeProvider(failing(503, times=10)), FakeProvider(["from backup"] * 3)
    client, gateway = fallback_client(settings, primary, backup)
    r = chat(client, model="smart")
    # the fallback answers even though the team only lists "smart"; fallbacks are the operator's choice
    assert r.status_code == 200 and r.json()["model"] == "cheap"
    assert backup.requests[0].model == "cheap-v2"
    assert len(primary.requests) == 2 and gateway.breakers["main"].state == "open"

    # with the breaker open, the next request skips the primary without calling it
    assert chat(client, model="smart").json()["model"] == "cheap"
    assert len(primary.requests) == 2


def test_no_fallback_for_a_bad_request(settings):
    primary, backup = FakeProvider(failing(400)), FakeProvider()
    client, _ = fallback_client(settings, primary, backup)
    r = chat(client, model="smart")
    assert r.status_code == 502 and backup.requests == []


def test_everything_down(settings):
    client, _ = fallback_client(settings, FakeProvider(failing(500, 5)), FakeProvider(failing(502, 5)))
    r = chat(client, model="smart")
    assert r.status_code == 503 and "fake returned 502" in r.json()["error"]["message"]


def test_missing_provider_key_only_breaks_that_model(settings):
    client, _ = make_client(settings)
    r = chat(client, key=OPS, model="gpt-big")
    assert r.status_code == 500 and error_code(r) == "provider_misconfigured"
    assert "NOT_SET_ANYWHERE" in r.json()["error"]["message"]
    assert chat(client, key=OPS).status_code == 200


def test_registry_prompt_instead_of_messages(settings):
    client, fake = make_client(settings)
    variables = {"product": "Acme", "article": "Reset it from settings.", "question": "How do I reset?"}
    r = chat(client, messages=None, model=None, prompt={"name": "support-reply", "variables": variables})
    assert r.status_code == 200 and r.json()["model"] == "gpt-5-mini"
    sent = fake.requests[0]
    assert sent.model == "gpt-5-mini-2025"
    assert "Reset it from settings." in " ".join(m.content for m in sent.messages)


def test_registry_prompt_errors(settings):
    client, _ = make_client(settings)
    r = chat(client, messages=None, model=None, prompt={"name": "support-reply", "variables": {}})
    assert (
        r.status_code == 400 and error_code(r) == "prompt_error" and "missing" in r.json()["error"]["message"]
    )
    r = chat(client, messages=None, model=None, prompt={"name": "nope"})
    assert r.status_code == 400 and error_code(r) == "prompt_error"


def test_models_lists_what_the_team_may_use(settings):
    client, _ = make_client(settings)
    r = client.get("/v1/models", headers={"Authorization": f"Bearer {SEARCH}"})
    assert [m["id"] for m in r.json()["data"]] == ["echo", "gpt-5-mini"]
    r = client.get("/v1/models", headers={"Authorization": f"Bearer {OPS}"})
    assert len(r.json()["data"]) == 3
    assert client.get("/v1/models").status_code == 401


def test_app_starts_without_any_config(client):
    assert client.get("/health").status_code == 200
    assert client.get("/v1/models", headers={"Authorization": "Bearer x"}).status_code == 401


def test_app_loads_config_file(tmp_path, settings):
    path = tmp_path / "gateway.yaml"
    path.write_text(
        "providers: {demo: {type: fake}}\n"
        "models: {echo: {provider: demo}}\n"
        f"teams: [{{name: t, key_sha256: {hash_key('k')}}}]\n"
    )
    client = TestClient(create_app(settings.model_copy(update={"config": str(path)})))
    r = chat(client, key="k")
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "echo: hello there"
