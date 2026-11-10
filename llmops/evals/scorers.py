"""
Scorers check one answer. Each one is a small class with a `name`, a pydantic
`Params` model for its settings in the suite file, and `score()`. Register a
new one with @register_scorer, or from another package through the
`llmops.scorers` entry point group.

Scorers here are deterministic on purpose: they're cheap, they don't need
another model call, and the same answer always gets the same score. They
catch a lot (a missing disclaimer, a made-up price, broken JSON). Judging
tone or helpfulness needs an LLM judge, which can plug in the same way.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from importlib.metadata import entry_points
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, ConfigDict, Field


class CheckResult(BaseModel):
    passed: bool
    detail: str = ""


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scorer(ABC):
    name: ClassVar[str]
    Params: ClassVar[type[BaseModel]]

    @abstractmethod
    def score(self, output: str, params: Any) -> CheckResult: ...


S = TypeVar("S", bound=type[Scorer])
_SCORERS: dict[str, type[Scorer]] = {}
_loaded_entry_points = False


def register_scorer(cls: S) -> S:
    if cls.name in _SCORERS and _SCORERS[cls.name] is not cls:
        raise ValueError(f"a scorer called '{cls.name}' is already registered")
    _SCORERS[cls.name] = cls
    return cls


def _load_entry_points() -> None:
    global _loaded_entry_points
    if not _loaded_entry_points:
        _loaded_entry_points = True
        for ep in entry_points(group="llmops.scorers"):
            _SCORERS.setdefault(ep.name, ep.load())


def available_scorers() -> list[str]:
    _load_entry_points()
    return sorted(_SCORERS)


def get_scorer(name: str) -> Scorer:
    _load_entry_points()
    try:
        return _SCORERS[name]()
    except KeyError:
        raise ValueError(
            f"unknown check type '{name}'. available: {', '.join(available_scorers())}"
        ) from None


def _fold(text: str, ignore_case: bool) -> str:
    return text.casefold() if ignore_case else text


class TextParams(Params):
    value: str = Field(min_length=1)
    ignore_case: bool = True


@register_scorer
class Contains(Scorer):
    name = "contains"
    Params = TextParams

    def score(self, output: str, params: TextParams) -> CheckResult:
        ok = _fold(params.value, params.ignore_case) in _fold(output, params.ignore_case)
        return CheckResult(passed=ok, detail="" if ok else f"missing '{params.value}'")


@register_scorer
class NotContains(Scorer):
    name = "not_contains"
    Params = TextParams

    def score(self, output: str, params: TextParams) -> CheckResult:
        ok = _fold(params.value, params.ignore_case) not in _fold(output, params.ignore_case)
        return CheckResult(passed=ok, detail="" if ok else f"contains '{params.value}'")


class AnyParams(Params):
    values: list[str] = Field(min_length=1)
    ignore_case: bool = True


@register_scorer
class ContainsAny(Scorer):
    """At least one of several phrasings, e.g. ["human colleague", "pass this on"]."""

    name = "contains_any"
    Params = AnyParams

    def score(self, output: str, params: AnyParams) -> CheckResult:
        text = _fold(output, params.ignore_case)
        ok = any(_fold(v, params.ignore_case) in text for v in params.values)
        return CheckResult(passed=ok, detail="" if ok else f"none of {params.values}")


class RegexParams(Params):
    pattern: str
    # must_match: false turns it into "must not match", e.g. for prices like \$\d+
    must_match: bool = True


@register_scorer
class Regex(Scorer):
    name = "regex"
    Params = RegexParams

    def score(self, output: str, params: RegexParams) -> CheckResult:
        found = re.search(params.pattern, output)
        if found is None:
            ok, detail = not params.must_match, f"no match for /{params.pattern}/"
        else:
            ok, detail = params.must_match, f"matched '{found.group(0)}'"
        return CheckResult(passed=ok, detail="" if ok else detail)


class LengthParams(Params):
    max_words: int = Field(gt=0)


@register_scorer
class MaxWords(Scorer):
    name = "max_words"
    Params = LengthParams

    def score(self, output: str, params: LengthParams) -> CheckResult:
        n = len(output.split())
        ok = n <= params.max_words
        return CheckResult(passed=ok, detail="" if ok else f"{n} words, limit {params.max_words}")


class JsonParams(Params):
    required_keys: list[str] = Field(default_factory=list)


@register_scorer
class ValidJson(Scorer):
    name = "json"
    Params = JsonParams

    def score(self, output: str, params: JsonParams) -> CheckResult:
        text = output.strip()
        # models like to wrap JSON in a ```json fence even when told not to
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            return CheckResult(passed=False, detail=f"not JSON: {e.msg}")
        missing = [k for k in params.required_keys if not isinstance(data, dict) or k not in data]
        if missing:
            return CheckResult(passed=False, detail=f"missing keys: {', '.join(missing)}")
        return CheckResult(passed=True)
