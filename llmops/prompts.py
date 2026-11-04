"""
Prompt registry: prompts live in YAML files under version control instead of
as strings scattered through app code. Each prompt has numbered versions, and
`active` says which one the gateway serves, so a new version can be added
(and evaluated) before it's switched on, and rolled back by changing one line.

    # prompts/support-reply.yaml
    name: support-reply
    description: Answer a customer question about our product
    active: 2
    versions:
      - version: 1
        model: gpt-5-mini
        messages:
          - {role: system, content: "You answer questions about {product}."}
          - {role: user, content: "{question}"}
      - version: 2
        ...

Placeholders use {name}. Write {{ and }} for literal braces.
"""

from __future__ import annotations

import string
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from llmops.providers.base import ChatMessage, CompletionRequest


class PromptError(ValueError):
    pass


def placeholders(text: str) -> set[str]:
    """Variable names used in a template. Raises PromptError for broken braces."""
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as e:
        raise PromptError(f"broken template {text[:40]!r}: {e}") from None
    names = set()
    for _, field, spec, conversion in parsed:
        if field is None:
            continue
        if not field.isidentifier() or spec or conversion:
            raise PromptError(f"placeholders must be plain names like {{product}}, got {{{field}}}")
        names.add(field)
    return names


class PromptVersion(BaseModel):
    version: int = Field(ge=1)
    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)
    note: str = ""

    @property
    def variables(self) -> set[str]:
        return set().union(*(placeholders(m.content) for m in self.messages))

    def render(self, /, **values: Any) -> list[ChatMessage]:
        needed = self.variables
        missing = needed - values.keys()
        if missing:
            raise PromptError(f"missing value(s) for: {', '.join(sorted(missing))}")
        unknown = values.keys() - needed
        if unknown:
            raise PromptError(
                f"unknown variable(s): {', '.join(sorted(unknown))} (the prompt uses {sorted(needed)})"
            )
        return [ChatMessage(role=m.role, content=m.content.format(**values)) for m in self.messages]

    def to_request(self, /, **values: Any) -> CompletionRequest:
        return CompletionRequest(
            model=self.model,
            messages=self.render(**values),
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )


class Prompt(BaseModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = ""
    active: int | None = None
    versions: list[PromptVersion] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> Prompt:
        numbers = [v.version for v in self.versions]
        if numbers != sorted(set(numbers)):
            raise ValueError("versions must be listed in increasing order, without duplicates")
        if self.active is not None and self.active not in numbers:
            raise ValueError(f"active version {self.active} doesn't exist (have {numbers})")
        for v in self.versions:
            v.variables  # noqa: B018  (raises on broken templates, so bad files fail at load time)
        return self

    @property
    def active_version(self) -> PromptVersion:
        return self.get(self.active) if self.active is not None else self.versions[-1]

    def get(self, version: int | None = None) -> PromptVersion:
        if version is None:
            return self.active_version
        for v in self.versions:
            if v.version == version:
                return v
        raise PromptError(f"{self.name} has no version {version}")


class PromptRegistry:
    def __init__(self, prompts: list[Prompt]):
        self._prompts: dict[str, Prompt] = {}
        for p in prompts:
            if p.name in self._prompts:
                raise PromptError(f"prompt '{p.name}' is defined twice")
            self._prompts[p.name] = p

    @classmethod
    def from_dir(cls, folder: str | Path) -> PromptRegistry:
        """Load every *.yaml / *.yml file in a folder. Any invalid file stops the load with a clear error."""
        folder = Path(folder)
        if not folder.is_dir():
            raise PromptError(f"prompt folder {folder} doesn't exist")
        prompts = []
        for path in sorted([*folder.glob("*.yaml"), *folder.glob("*.yml")]):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8"))
                prompts.append(Prompt.model_validate(data))
            except (yaml.YAMLError, ValidationError, PromptError) as e:
                raise PromptError(f"{path.name}: {e}") from None
        return cls(prompts)

    def names(self) -> list[str]:
        return sorted(self._prompts)

    def __getitem__(self, name: str) -> Prompt:
        try:
            return self._prompts[name]
        except KeyError:
            raise PromptError(
                f"no prompt called '{name}'. known: {', '.join(self.names()) or 'none'}"
            ) from None

    def render(
        self, prompt_name: str, values: dict[str, Any] | None = None, version: int | None = None
    ) -> CompletionRequest:
        """The request for a prompt (its active version unless `version` is given) with `values` filled in.

        Values are a dict rather than **kwargs so a prompt can use any variable name, `name` included.
        """
        return self[prompt_name].get(version).to_request(**(values or {}))
