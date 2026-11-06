import io
import json
import logging

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_gateway import SEARCH, chat, fallback_client, make_client

from llmops.observability import JsonFormatter, request_id_from, request_id_var
from llmops.providers.fake import FakeProvider, failing

_exporter = InMemorySpanExporter()


@pytest.fixture(scope="session", autouse=True)
def tracer_provider():
    # the global provider can only be set once per process, so the whole session shares it
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_exporter))
    trace.set_tracer_provider(provider)


@pytest.fixture
def spans():
    _exporter.clear()
    return _exporter


@pytest.fixture
def json_logs():
    """Everything the llmops loggers write during a test, as parsed JSON lines."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("llmops")
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.INFO)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    logger.removeHandler(handler)
    logger.setLevel(old_level)


def test_json_formatter_includes_extras_and_request_id():
    record = logging.LogRecord("llmops", logging.WARNING, __file__, 1, "hello %s", ("world",), None)
    record.team = "search"
    token = request_id_var.set("req-1")
    try:
        entry = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert entry["message"] == "hello world" and entry["level"] == "warning"
    assert entry["team"] == "search" and entry["request_id"] == "req-1"
    assert entry["ts"].endswith("+00:00")


def test_json_formatter_includes_the_traceback():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "failed", None, sys.exc_info())
    entry = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in entry["error"]


@pytest.mark.parametrize(
    "header, kept",
    [
        ("abc-123", True),
        ("trace:7f.1_x", True),
        ("has space", False),
        ("x\ninjected", False),
        ("a" * 200, False),
    ],
)
def test_request_id_from_header(header, kept):
    rid = request_id_from(header)
    assert (rid == header) is kept and rid


def test_request_id_is_echoed_or_made_up(settings):
    client, _ = make_client(settings)
    assert client.get("/health", headers={"x-request-id": "abc-123"}).headers["x-request-id"] == "abc-123"
    made_up = client.get("/health").headers["x-request-id"]
    assert len(made_up) == 32


def test_access_log_line(settings, json_logs):
    client, _ = make_client(settings)
    r = chat(client, model="gpt-5-mini")
    rid = r.headers["x-request-id"]
    access = [e for e in json_logs() if e["message"] == "request"]
    assert len(access) == 1
    line = access[0]
    assert line["request_id"] == rid and line["status"] == 200
    assert line["team"] == "search" and line["model"] == "gpt-5-mini"
    assert line["path"] == "/v1/chat/completions" and line["duration_ms"] >= 0
    assert len(line["trace_id"]) == 32


def test_fallback_is_logged_with_the_request_id(settings, json_logs):
    client, _ = fallback_client(settings, FakeProvider(failing(503, 5)), FakeProvider(["ok"]))
    r = chat(client, model="smart")
    warning = next(e for e in json_logs() if e["message"] == "falling back")
    assert warning["request_id"] == r.headers["x-request-id"]
    assert warning["model"] == "smart" and warning["fallback"] == "cheap"


def test_spans_for_the_request_and_the_provider_call(settings, spans):
    client, _ = make_client(settings)
    chat(client, model="gpt-5-mini")
    finished = spans.get_finished_spans()
    # recent FastAPI versions add spans of their own in between, so look for ours by name
    server = next(s for s in finished if s.name == "POST /v1/chat/completions" and s.parent is None)
    llm = next(s for s in finished if s.name == "chat gpt-5-mini-2025")
    assert llm.context.trace_id == server.context.trace_id
    assert llm.attributes["gen_ai.request.model"] == "gpt-5-mini-2025"
    assert llm.attributes["llmops.model"] == "gpt-5-mini"
    assert llm.attributes["gen_ai.usage.output_tokens"] > 0
    assert server.attributes["http.response.status_code"] == 200


def test_failed_attempts_are_error_spans(settings, spans):
    client, _ = fallback_client(settings, FakeProvider(failing(503, 5)), FakeProvider(["ok"]))
    chat(client, model="smart")
    llm = [s for s in spans.get_finished_spans() if s.name.startswith("chat ")]
    assert [(s.name, s.status.status_code.name) for s in llm] == [
        ("chat smart-v1", "ERROR"),
        ("chat cheap-v2", "UNSET"),
    ]
    assert llm[1].attributes["llmops.fallback"] is True


def test_a_search_key_is_never_logged(settings, json_logs):
    client, _ = make_client(settings)
    chat(client)
    assert SEARCH not in json.dumps(json_logs())
