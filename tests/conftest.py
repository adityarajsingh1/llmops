import pytest
from fastapi.testclient import TestClient

from llmops.app import create_app
from llmops.settings import Settings


@pytest.fixture
def settings() -> Settings:
    # built explicitly so a developer's own .env or LLMOPS_* variables can't leak into tests
    return Settings(_env_file=None, environment="dev", instance_name="test-gateway")


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))
