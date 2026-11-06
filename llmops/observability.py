"""
Logs and traces. Every request gets an id (taken from an incoming
X-Request-ID header or made up), which shows up in each log line, in the
response headers and on the trace, so one slow or failed call can be followed
from the app that made it down to the provider.

Logs are one JSON object per line, ready for Loki, Elasticsearch or
CloudWatch. Traces use OpenTelemetry: the gateway always creates spans, and
they're exported over OTLP when LLMOPS_OTEL_ENDPOINT is set (needs the
`otel` extra). Without it, the spans cost next to nothing.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from fastapi import Request, Response
from opentelemetry import trace

from llmops.settings import Settings

log = logging.getLogger("llmops")
tracer = trace.get_tracer("llmops")

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# attributes every LogRecord has; anything else came in through `extra=` and goes into the JSON
_STANDARD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {
    "message",
    "asctime",
    "color_message",  # uvicorn adds an ANSI-colored copy of the message
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id:
            entry["request_id"] = request_id
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            entry["trace_id"] = format(span.trace_id, "032x")
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                entry[key] = value
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(settings.log_level.upper())


def configure_tracing(settings: Settings) -> None:
    if not settings.otel_endpoint:
        return
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        raise RuntimeError(
            "LLMOPS_OTEL_ENDPOINT is set but the exporter isn't installed: pip install 'llmops[otel]'"
        ) from None
    provider = TracerProvider(
        resource=Resource.create({"service.name": "llmops", "service.instance.id": settings.instance_name})
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint)))
    trace.set_tracer_provider(provider)


def request_id_from(header: str | None) -> str:
    """Keep the caller's id if it looks sane (so ids can't be used to inject into logs), else make one."""
    if header and _VALID_REQUEST_ID.match(header):
        return header
    return uuid.uuid4().hex


async def observe_request(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """HTTP middleware: request id, a server span and one access log line per request."""
    request_id = request_id_from(request.headers.get("x-request-id"))
    token = request_id_var.set(request_id)
    started = time.perf_counter()
    name = f"{request.method} {request.url.path}"
    try:
        with tracer.start_as_current_span(name, kind=trace.SpanKind.SERVER) as span:
            span.set_attribute("http.request.method", request.method)
            span.set_attribute("url.path", request.url.path)
            span.set_attribute("llmops.request_id", request_id)
            try:
                response = await call_next(request)
            except Exception:
                log.exception("request failed", extra={"method": request.method, "path": request.url.path})
                raise
            span.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                span.set_status(trace.StatusCode.ERROR)
            response.headers["x-request-id"] = request_id
            # the gateway endpoints leave the team and model on request.state for this line
            fields = {
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                "team": getattr(request.state, "team", None),
                "model": getattr(request.state, "model", None),
            }
            log.info("request", extra={k: v for k, v in fields.items() if v is not None})
            return response
    finally:
        request_id_var.reset(token)
