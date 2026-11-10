"""
Eval suite files (evals/*.yaml):

    prompt: support-reply
    checks:                      # run on every case
      - {type: not_contains, value: "as an AI"}
    cases:
      - id: reset-password
        variables: {product: Acme, article: "...", question: "How do I reset my password?"}
        checks:
          - {type: contains, value: "Settings"}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from llmops.evals.scorers import get_scorer


class SuiteError(Exception):
    pass


class Check(BaseModel):
    """One check: a scorer name plus its settings, validated when the suite is loaded."""

    model_config = ConfigDict(extra="allow")

    type: str

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.model_extra or {})

    @model_validator(mode="after")
    def _known_type_and_params(self) -> Check:
        scorer = get_scorer(self.type)
        scorer.Params.model_validate(self.params)
        return self

    def run(self, output: str) -> tuple[bool, str]:
        scorer = get_scorer(self.type)
        result = scorer.score(output, scorer.Params.model_validate(self.params))
        return result.passed, result.detail

    def label(self) -> str:
        shown = ", ".join(f"{k}={v!r}" for k, v in self.params.items())
        return f"{self.type}({shown})"


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    variables: dict[str, Any] = Field(default_factory=dict)
    checks: list[Check] = Field(default_factory=list)


class EvalSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    description: str = ""
    checks: list[Check] = Field(default_factory=list)
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_cases(self) -> EvalSuite:
        ids = [c.id for c in self.cases]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate case ids: {', '.join(dupes)}")
        for case in self.cases:
            if not case.checks and not self.checks:
                raise ValueError(f"case '{case.id}' has no checks")
        return self

    def checks_for(self, case: Case) -> list[Check]:
        return [*self.checks, *case.checks]

    @classmethod
    def load(cls, path: str | Path) -> EvalSuite:
        path = Path(path)
        try:
            return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except OSError as e:
            raise SuiteError(f"can't read {path}: {e.strerror}") from None
        except (yaml.YAMLError, ValidationError) as e:
            raise SuiteError(f"{path.name}: {e}") from None
