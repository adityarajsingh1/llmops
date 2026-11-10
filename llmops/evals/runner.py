"""Runs a suite: render the prompt for each case, ask the model, score the answer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from pydantic import BaseModel, Field, computed_field

from llmops.evals.suite import Case, EvalSuite
from llmops.prompts import PromptError, PromptRegistry
from llmops.providers import CompletionRequest, CompletionResponse, ProviderError

# (model name, request) -> response; the CLI passes Gateway.complete wrapped to this shape
Complete = Callable[[str, CompletionRequest], Awaitable[CompletionResponse]]


class CheckOutcome(BaseModel):
    check: str
    passed: bool
    detail: str = ""


class CaseResult(BaseModel):
    id: str
    output: str | None = None
    error: str | None = None
    checks: list[CheckOutcome] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> float:
        """Share of checks passed; a case that errored scores 0."""
        if self.error is not None or not self.checks:
            return 0.0
        return sum(c.passed for c in self.checks) / len(self.checks)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed for c in self.checks)


class SuiteResult(BaseModel):
    prompt: str
    version: int
    model: str
    started_at: datetime
    cases: list[CaseResult]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score(self) -> float:
        return sum(c.score for c in self.cases) / len(self.cases) if self.cases else 0.0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def pass_rate(self) -> float:
        return sum(c.passed for c in self.cases) / len(self.cases) if self.cases else 0.0


def _values_for(case: Case, used: set[str], all_versions_use: set[str]) -> dict[str, object]:
    # a suite is shared by all versions of a prompt, so a variable only the newer version
    # uses (like `article`) is dropped for the older one; anything else unknown is a typo
    # and still fails in render()
    return {k: v for k, v in case.variables.items() if k in used or k not in all_versions_use}


async def run_suite(
    suite: EvalSuite,
    prompts: PromptRegistry,
    complete: Complete,
    *,
    version: int | None = None,
    model: str | None = None,
    concurrency: int = 4,
) -> SuiteResult:
    prompt = prompts[suite.prompt]
    pv = prompt.get(version)
    target = model or pv.model
    every_variable = set().union(*(v.variables for v in prompt.versions))
    limit = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> CaseResult:
        try:
            request = pv.to_request(**_values_for(case, pv.variables, every_variable))
        except PromptError as e:
            return CaseResult(id=case.id, error=f"prompt: {e}")
        request = request.model_copy(update={"model": target})
        async with limit:
            try:
                resp = await complete(target, request)
            except ProviderError as e:
                return CaseResult(id=case.id, error=f"model: {e}")
        outcomes = []
        for check in suite.checks_for(case):
            passed, detail = check.run(resp.text)
            outcomes.append(CheckOutcome(check=check.label(), passed=passed, detail=detail))
        return CaseResult(id=case.id, output=resp.text, checks=outcomes)

    started = datetime.now(timezone.utc)
    results = await asyncio.gather(*(one(c) for c in suite.cases))
    return SuiteResult(
        prompt=suite.prompt, version=pv.version, model=target, started_at=started, cases=results
    )
