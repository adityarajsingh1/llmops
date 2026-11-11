FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# dependencies first, so code changes don't reinstall everything
COPY pyproject.toml README.md LICENSE ./
COPY llmops ./llmops
RUN pip install ".[otel]"

COPY prompts ./prompts

RUN useradd --create-home --uid 10001 llmops && mkdir /data && chown llmops /data
USER llmops

ENV LLMOPS_HOST=0.0.0.0 \
    LLMOPS_PORT=8000 \
    LLMOPS_CONFIG=/app/gateway.yaml \
    LLMOPS_PROMPTS_DIR=/app/prompts \
    LLMOPS_USAGE_DB=/data/llmops-usage.db

EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["llmops", "serve"]
