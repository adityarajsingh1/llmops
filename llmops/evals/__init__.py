"""
Offline evals for prompts: a suite is a list of cases (variables for a prompt,
plus checks the answer must pass). Run a suite before and after a prompt
change and compare the scores, so a change that makes answers worse fails
in CI instead of in front of customers.
"""

from llmops.evals.runner import CaseResult, CheckOutcome, SuiteResult, run_suite
from llmops.evals.scorers import CheckResult, Scorer, available_scorers, register_scorer
from llmops.evals.suite import Case, Check, EvalSuite, SuiteError

__all__ = [
    "Case",
    "CaseResult",
    "Check",
    "CheckOutcome",
    "CheckResult",
    "EvalSuite",
    "Scorer",
    "SuiteError",
    "SuiteResult",
    "available_scorers",
    "register_scorer",
    "run_suite",
]
