"""`llmops eval run` and `llmops eval compare`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from llmops.config import ConfigError, GatewayConfig
from llmops.evals.compare import EvalRun, compare_runs, to_markdown
from llmops.evals.runner import SuiteResult, run_suite
from llmops.evals.suite import EvalSuite, SuiteError
from llmops.gateway import Gateway, GatewayError
from llmops.prompts import PromptError, PromptRegistry
from llmops.providers import CompletionRequest, CompletionResponse, ProviderError
from llmops.settings import get_settings


def eval_command(args: argparse.Namespace) -> int:
    try:
        if args.action == "run":
            return asyncio.run(_run(args))
        if args.action == "check":
            return _check(args)
        return _compare(args)
    except (SuiteError, PromptError, ConfigError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    prompts = PromptRegistry.from_dir(args.prompts or settings.prompts_dir)
    suites = [EvalSuite.load(p) for p in args.suites]
    if args.skip_missing:
        for s in suites:
            if s.prompt not in prompts.names():
                print(f"skipping {s.prompt}: no such prompt in {args.prompts or settings.prompts_dir}")
        suites = [s for s in suites if s.prompt in prompts.names()]
    gateway = Gateway(GatewayConfig.load(args.config or settings.config), prompts)

    for suite in suites:
        target = args.model or prompts[suite.prompt].get(args.version).model
        if target not in gateway.config.models:
            raise ConfigError(f"{suite.prompt} uses model '{target}', which isn't in the gateway config")

    async def complete(model: str, request: CompletionRequest) -> CompletionResponse:
        # the same path as live traffic: retries, fallbacks and the breaker all apply
        try:
            return (await gateway.complete(model, request))[1]
        except GatewayError as e:
            raise ProviderError(e.message, provider="gateway", retryable=e.status >= 500) from None

    results: list[SuiteResult] = []
    try:
        for suite in suites:
            result = await run_suite(suite, prompts, complete, version=args.version, model=args.model)
            results.append(result)
            _print_result(result)
    finally:
        await gateway.aclose()

    if args.out:
        Path(args.out).write_text(EvalRun(results=results).model_dump_json(indent=2), encoding="utf-8")
    low = [r for r in results if r.score < args.min_score]
    for r in low:
        print(
            f"FAIL: {r.prompt} scored {r.score:.1%}, below --min-score {args.min_score:.1%}", file=sys.stderr
        )
    return 1 if low else 0


def _check(args: argparse.Namespace) -> int:
    prompts = PromptRegistry.from_dir(args.prompts or get_settings().prompts_dir)
    for path in args.suites:
        suite = EvalSuite.load(path)
        prompts[suite.prompt]  # raises if the prompt doesn't exist
        print(f"ok: {path} ({suite.prompt}, {len(suite.cases)} cases)")
    return 0


def _print_result(result: SuiteResult) -> None:
    passed = sum(c.passed for c in result.cases)
    print(
        f"{result.prompt} v{result.version} on {result.model}: score {result.score:.1%}, "
        f"{passed}/{len(result.cases)} cases passed"
    )
    for case in result.cases:
        if case.error:
            print(f"  ERROR {case.id}: {case.error}")
        for c in case.checks:
            if not c.passed:
                print(f"  FAIL  {case.id}: {c.check}: {c.detail}")


def _compare(args: argparse.Namespace) -> int:
    def load(path: str) -> EvalRun:
        try:
            return EvalRun.model_validate_json(Path(path).read_text(encoding="utf-8"))
        except OSError as e:
            raise SuiteError(f"can't read {path}: {e.strerror}") from None
        except ValueError as e:
            raise SuiteError(f"{path} isn't an eval results file: {e}") from None

    comparisons = compare_runs(load(args.base), load(args.new), args.max_drop)
    print(to_markdown(comparisons, args.max_drop))
    return 0 if all(c.ok for c in comparisons) else 1
