"""The FastAPI app. create_app() builds a fresh one, which keeps tests independent."""

import time

from fastapi import FastAPI
from pydantic import BaseModel

from llmops import __version__
from llmops.settings import Settings, get_settings


class Health(BaseModel):
    status: str
    version: str
    environment: str
    instance: str
    uptime_seconds: float


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="llmops gateway",
        version=__version__,
        description="One place in front of your LLM providers: budgets, prompt versions, tracing, metrics.",
    )
    app.state.settings = settings
    started = time.monotonic()

    @app.get("/health", response_model=Health, tags=["ops"])
    def health() -> Health:
        """Liveness check for load balancers and docker-compose."""
        return Health(
            status="ok",
            version=__version__,
            environment=settings.environment,
            instance=settings.instance_name,
            uptime_seconds=round(time.monotonic() - started, 3),
        )

    return app
