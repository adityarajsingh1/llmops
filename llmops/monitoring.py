"""
Quality monitoring on live traffic. Offline evals catch a bad prompt change
before it ships; they can't catch a provider quietly changing the model under
the same name, or real users asking things nobody wrote a test case for.

So the gateway can keep a small random sample of real answers (off by
default, see `sample_rate`) and run monitors on them. A monitor is a set of
the same checks the eval suites use (llmops.evals.scorers), applied to every
sampled answer of one prompt or model. Results go to Prometheus as they
happen, and the samples are kept for the weekly report.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator

from llmops.evals.suite import Check


class Monitor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    # which answers it looks at; with neither, every sampled answer
    prompt: str | None = None
    model: str | None = None
    checks: list[Check] = Field(min_length=1)

    def applies_to(self, model: str, prompt: str | None) -> bool:
        return (self.prompt is None or self.prompt == prompt) and (self.model is None or self.model == model)

    def run(self, output: str) -> bool:
        return all(check.run(output)[0] for check in self.checks)


class MonitoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # share of answered requests to keep, 0 to 1. Samples contain model output, so think about
    # what your users send before turning this on.
    sample_rate: float = Field(default=0.0, ge=0, le=1)
    monitors: list[Monitor] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_names(self) -> MonitoringConfig:
        names = [m.name for m in self.monitors]
        if len(names) != len(set(names)):
            raise ValueError("monitor names must be unique")
        return self


class Sample(BaseModel):
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    team: str
    model: str
    prompt: str | None = None
    prompt_version: int | None = None
    output: str
    completion_tokens: int
    finish_reason: str | None = None
    # monitor name -> passed
    checks: dict[str, bool] = Field(default_factory=dict)


def run_monitors(monitors: list[Monitor], sample: Sample) -> dict[str, bool]:
    return {m.name: m.run(sample.output) for m in monitors if m.applies_to(sample.model, sample.prompt)}
