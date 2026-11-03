import pytest
from pydantic import ValidationError

from llmops.settings import Settings


def test_defaults():
    s = Settings(_env_file=None)
    assert (s.environment, s.host, s.port) == ("dev", "127.0.0.1", 8000)


def test_env_vars(monkeypatch):
    monkeypatch.setenv("LLMOPS_PORT", "9100")
    monkeypatch.setenv("LLMOPS_ENVIRONMENT", "prod")
    s = Settings(_env_file=None)
    assert s.port == 9100 and s.environment == "prod"


def test_dotenv_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text("LLMOPS_INSTANCE_NAME=eu-1\nLLMOPS_LOG_LEVEL=debug\n")
    s = Settings(_env_file=env)
    assert s.instance_name == "eu-1" and s.log_level == "debug"


@pytest.mark.parametrize(
    "field, value", [("port", 70000), ("environment", "production"), ("log_level", "loud")]
)
def test_bad_values_fail_at_startup(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
