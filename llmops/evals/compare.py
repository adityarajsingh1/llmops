"""Compare two eval runs of the same suites, e.g. the base branch's prompts against a pull request's."""

from __future__ import annotations

from pydantic import BaseModel, Field

from llmops.evals.runner import SuiteResult


class EvalRun(BaseModel):
    """What `llmops eval run --out` writes: one result per suite."""

    results: list[SuiteResult]


class CaseChange(BaseModel):
    id: str
    before: float | None
    after: float | None


class SuiteComparison(BaseModel):
    prompt: str
    before: float
    after: float
    regressions: list[CaseChange] = Field(default_factory=list)
    fixes: list[CaseChange] = Field(default_factory=list)
    ok: bool

    @property
    def delta(self) -> float:
        return self.after - self.before


def compare_suite(base: SuiteResult, new: SuiteResult, max_drop: float) -> SuiteComparison:
    old_scores = {c.id: c.score for c in base.cases}
    new_scores = {c.id: c.score for c in new.cases}
    regressions, fixes = [], []
    for case_id in sorted(old_scores.keys() | new_scores.keys()):
        before, after = old_scores.get(case_id), new_scores.get(case_id)
        if before is not None and after is not None and after < before:
            regressions.append(CaseChange(id=case_id, before=before, after=after))
        elif before is not None and after is not None and after > before:
            fixes.append(CaseChange(id=case_id, before=before, after=after))
    # small tolerance so float noise in averages can't fail a run on its own
    ok = new.score >= base.score - max_drop - 1e-9
    return SuiteComparison(
        prompt=new.prompt, before=base.score, after=new.score, regressions=regressions, fixes=fixes, ok=ok
    )


def compare_runs(base: EvalRun, new: EvalRun, max_drop: float = 0.0) -> list[SuiteComparison]:
    """One comparison per prompt in the new run. A suite missing from the base run is compared
    against nothing and passes, so adding a new suite doesn't fail the check."""
    before = {r.prompt: r for r in base.results}
    out = []
    for result in new.results:
        old = before.get(result.prompt)
        if old is None:
            out.append(
                SuiteComparison(prompt=result.prompt, before=result.score, after=result.score, ok=True)
            )
        else:
            out.append(compare_suite(old, result, max_drop))
    return out


def to_markdown(comparisons: list[SuiteComparison], max_drop: float) -> str:
    lines = [
        "| prompt | before | after | change | |",
        "|---|---|---|---|---|",
    ]
    for c in comparisons:
        mark = "ok" if c.ok else "**worse**"
        lines.append(f"| {c.prompt} | {c.before:.1%} | {c.after:.1%} | {c.delta:+.1%} | {mark} |")
    for c in comparisons:
        if c.regressions:
            lines.append(f"\n{c.prompt}, cases that got worse:")
            lines += [f"- `{r.id}`: {r.before:.0%} -> {r.after:.0%}" for r in c.regressions]
    lines.append(f"\nAllowed drop: {max_drop:.1%}")
    return "\n".join(lines)
