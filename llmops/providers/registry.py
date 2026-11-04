"""
Provider registry. Built-in providers register themselves with @register,
and third-party packages can add their own through the `llmops.providers`
entry point group, without changing this repo:

    # in the other package's pyproject.toml
    [project.entry-points."llmops.providers"]
    mistral = "llmops_mistral:MistralProvider"
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any, TypeVar

from llmops.providers.base import Provider

P = TypeVar("P", bound=type[Provider])

_PROVIDERS: dict[str, type[Provider]] = {}
_entry_points_loaded = False


def register(cls: P) -> P:
    """Class decorator: make a provider available under its `name`."""
    name = getattr(cls, "name", None)
    if not name:
        raise TypeError(f"{cls.__name__} needs a `name` to be registered")
    if name in _PROVIDERS and _PROVIDERS[name] is not cls:
        raise ValueError(f"a provider called '{name}' is already registered ({_PROVIDERS[name].__name__})")
    _PROVIDERS[name] = cls
    return cls


def _load_entry_points() -> None:
    global _entry_points_loaded
    if _entry_points_loaded:
        return
    _entry_points_loaded = True
    for ep in entry_points(group="llmops.providers"):
        cls = ep.load()
        if not (isinstance(cls, type) and issubclass(cls, Provider)):
            raise TypeError(f"entry point {ep.name} doesn't point at a Provider subclass")
        _PROVIDERS.setdefault(ep.name, cls)


def available() -> list[str]:
    _load_entry_points()
    return sorted(_PROVIDERS)


def create(name: str, **config: Any) -> Provider:
    """Build a provider by name, e.g. create("openai", api_key="sk-...")."""
    _load_entry_points()
    try:
        cls: Callable[..., Provider] = _PROVIDERS[name]
    except KeyError:
        raise ValueError(f"unknown provider '{name}'. available: {', '.join(available())}") from None
    return cls(**config)
