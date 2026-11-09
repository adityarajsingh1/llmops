"""The FastAPI app. create_app() builds a fresh one, which keeps tests independent."""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel

from llmops import __version__
from llmops.config import GatewayConfig
from llmops.gateway import Gateway, GatewayError, gateway_error_handler, router, validation_error_handler
from llmops.observability import configure_logging, configure_tracing, observe_request
from llmops.prompts import PromptRegistry
from llmops.settings import Settings, get_settings
from llmops.usage import open_store


class Health(BaseModel):
    status: str
    version: str
    environment: str
    instance: str
    uptime_seconds: float


def load_gateway(settings: Settings) -> Gateway:
    """The gateway as described by the config file and prompt folder. Both are optional, so the
    app still starts (and answers /health) before anything is configured."""
    config = GatewayConfig.load(settings.config) if Path(settings.config).is_file() else GatewayConfig()
    prompts = PromptRegistry.from_dir(settings.prompts_dir) if Path(settings.prompts_dir).is_dir() else None
    return Gateway(config, prompts, usage=open_store(settings.usage_db))


def create_app(settings: Settings | None = None, gateway: Gateway | None = None) -> FastAPI:
    settings = settings or get_settings()
    gw = gateway or load_gateway(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await gw.aclose()

    app = FastAPI(
        title="llmops gateway",
        version=__version__,
        description="One place in front of your LLM providers: budgets, prompt versions, tracing, metrics.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.gateway = gw
    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.middleware("http")(observe_request)
    app.include_router(router)
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


def serve_app() -> FastAPI:
    """What `llmops serve` runs: create_app() plus process-wide logging and tracing setup, which
    tests and embedding apps shouldn't get by just importing the app."""
    settings = get_settings()
    configure_logging(settings)
    configure_tracing(settings)
    return create_app(settings)
