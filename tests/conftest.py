import pytest
from fastapi.testclient import TestClient

from llmops.app import create_app
from llmops.settings import Settings


@pytest.fixture
def settings(tmp_path) -> Settings:
    # built explicitly so a developer's own .env, gateway.yaml or LLMOPS_* variables can't leak into tests
    return Settings(
        _env_file=None, environment="dev", instance_name="test-gateway", config=str(tmp_path / "none.yaml")
    )


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))
