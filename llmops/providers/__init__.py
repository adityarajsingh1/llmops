"""LLM providers behind one interface. See base.py for the interface and registry.py for plugins."""

from llmops.providers import fake, openai  # noqa: F401  (registers the built-in providers)
from llmops.providers.base import (
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    Provider,
    ProviderError,
    Usage,
)
from llmops.providers.registry import available, create, register

__all__ = [
    "ChatMessage",
    "CompletionRequest",
    "CompletionResponse",
    "Provider",
    "ProviderError",
    "Usage",
    "available",
    "create",
    "register",
]
