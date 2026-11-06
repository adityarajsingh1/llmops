import pytest

from llmops.auth import hash_key
from llmops.config import ConfigError, GatewayConfig
from llmops.providers.fake import FakeProvider

H1, H2 = hash_key("one"), hash_key("two")


def config(**overrides):
    data = {
        "providers": {"demo": {"type": "fake"}},
        "models": {"echo": {"provider": "demo"}},
        "teams": [{"name": "search", "key_sha256": H1, "models": ["echo"]}],
    }
    data.update(overrides)
    return data


def test_valid_config():
    cfg = GatewayConfig.model_validate(config())
    assert cfg.teams[0].may_use("echo") and not cfg.teams[0].may_use("other")
    assert isinstance(cfg.providers["demo"].build(), FakeProvider)


def test_team_without_models_may_use_all():
    cfg = GatewayConfig.model_validate(config(teams=[{"name": "ops", "key_sha256": H1}]))
    assert cfg.teams[0].may_use("anything")


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"models": {"echo": {"provider": "nope"}}}, "provider 'nope'"),
        ({"teams": [{"name": "a", "key_sha256": H1, "models": ["gpt"]}]}, "unknown model"),
        ({"teams": [{"name": "a", "key_sha256": H1}, {"name": "a", "key_sha256": H2}]}, "defined twice"),
        ({"teams": [{"name": "a", "key_sha256": H1}, {"name": "b", "key_sha256": H1}]}, "reuses"),
        ({"teams": [{"name": "a", "key_sha256": "plaintext-key"}]}, "key_sha256"),
        ({"extra": 1}, "extra"),
    ],
)
def test_bad_configs(overrides, message):
    with pytest.raises(ValueError, match=message):
        GatewayConfig.model_validate(config(**overrides))


def test_api_key_comes_from_the_environment(monkeypatch):
    cfg = GatewayConfig.model_validate(
        config(providers={"oa": {"type": "openai", "api_key_env": "TEST_OA_KEY"}}, models={}, teams=[])
    )
    with pytest.raises(ConfigError, match="TEST_OA_KEY"):
        cfg.providers["oa"].build()
    monkeypatch.setenv("TEST_OA_KEY", "sk-test")
    assert cfg.providers["oa"].build().name == "openai"


def test_load_reports_the_file(tmp_path):
    path = tmp_path / "gateway.yaml"
    path.write_text("models: {echo: {provider: missing}}\n")
    with pytest.raises(ConfigError, match="gateway.yaml"):
        GatewayConfig.load(path)
    with pytest.raises(ConfigError, match="can't read"):
        GatewayConfig.load(tmp_path / "nope.yaml")


def test_example_config_is_valid():
    from pathlib import Path

    GatewayConfig.load(Path(__file__).resolve().parent.parent / "gateway.example.yaml")


@pytest.mark.parametrize("fallbacks", [["echo"], ["missing"]])
def test_bad_fallbacks(fallbacks):
    with pytest.raises(ValueError, match="invalid fallback"):
        GatewayConfig.model_validate(config(models={"echo": {"provider": "demo", "fallbacks": fallbacks}}))


def test_reliability_settings():
    cfg = GatewayConfig.model_validate(config(reliability={"retry": {"attempts": 5, "timeout": 10}}))
    assert cfg.reliability.retry.attempts == 5 and cfg.reliability.breaker.failure_threshold == 5
    with pytest.raises(ValueError):
        GatewayConfig.model_validate(config(reliability={"retry": {"attempts": 0}}))
