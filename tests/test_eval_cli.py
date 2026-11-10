"""The eval CLI end to end, with the fake provider: it echoes the last user message, so a
prompt change shows up in the answers and the scores move for real."""

import json

import pytest

from llmops import cli
from llmops.evals.compare import EvalRun, compare_runs, to_markdown
from llmops.evals.runner import CaseResult, CheckOutcome, SuiteResult

PROMPT_V1 = """
name: greet
versions:
  - version: 1
    model: echo
    messages:
      - {role: user, content: "Hello {name}, welcome to {product}!"}
"""

# a "change" that drops the product name from the answer
PROMPT_V2 = (
    PROMPT_V1.replace("active: 1\n", "")
    + """
  - version: 2
    model: echo
    messages:
      - {role: user, content: "Hello {name}!"}
"""
)

SUITE = """
prompt: greet
checks:
  - {type: contains, value: "hello"}
cases:
  - id: ana
    variables: {name: Ana, product: Acme}
    checks: [{type: contains, value: Acme}]
  - id: bo
    variables: {name: Bo, product: Initech}
    checks: [{type: contains, value: Initech}]
"""

CONFIG = """
providers: {demo: {type: fake}}
models: {echo: {provider: demo}}
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "greet.yaml").write_text(PROMPT_V2)
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "greet.yaml").write_text(SUITE)
    (tmp_path / "gateway.yaml").write_text(CONFIG)
    return tmp_path


def run_cli(*args):
    with pytest.raises(SystemExit) as exit_:
        cli.main(list(args))
    return exit_.value.code


def eval_run(project, *extra):
    suite, prompts, config = project / "evals" / "greet.yaml", project / "prompts", project / "gateway.yaml"
    return run_cli("eval", "run", str(suite), "--prompts", str(prompts), "--config", str(config), *extra)


def test_run_prints_scores_and_failures(project, capsys):
    assert eval_run(project, "--version", "1") == 0
    assert "greet v1 on echo: score 100.0%, 2/2 cases passed" in capsys.readouterr().out

    assert eval_run(project) == 0  # v2, the newest, since there's no `active`
    out = capsys.readouterr().out
    assert "score 50.0%, 0/2 cases passed" in out
    assert "FAIL  ana: contains(value='Acme')" in out


def test_min_score(project, capsys):
    assert eval_run(project, "--min-score", "0.9") == 1
    assert "below --min-score" in capsys.readouterr().err


def test_compare_catches_the_regression(project, tmp_path, capsys):
    base, new = tmp_path / "base.json", tmp_path / "new.json"
    assert eval_run(project, "--version", "1", "--out", str(base)) == 0
    assert eval_run(project, "--version", "2", "--out", str(new)) == 0
    assert json.loads(base.read_text())["results"][0]["score"] == 1.0
    capsys.readouterr()

    assert run_cli("eval", "compare", str(base), str(new)) == 1
    out = capsys.readouterr().out
    assert "| greet | 100.0% | 50.0% | -50.0% | **worse** |" in out
    assert "- `ana`: 100% -> 50%" in out

    assert run_cli("eval", "compare", str(base), str(new), "--max-drop", "0.5") == 0
    assert run_cli("eval", "compare", str(new), str(base)) == 0  # getting better is fine


def test_unknown_model_is_a_clear_error(project, capsys):
    assert eval_run(project, "--model", "gpt-9") == 1
    assert "uses model 'gpt-9', which isn't in the gateway config" in capsys.readouterr().err


def test_bad_results_file(tmp_path, capsys):
    bad = tmp_path / "x.json"
    bad.write_text("{}")
    assert run_cli("eval", "compare", str(bad), str(bad)) == 1
    assert "isn't an eval results file" in capsys.readouterr().err


def result(prompt, *scores):
    cases = [
        CaseResult(id=f"c{i}", output="x", checks=[CheckOutcome(check="c", passed=bool(s))])
        for i, s in enumerate(scores)
    ]
    return SuiteResult(prompt=prompt, version=1, model="m", started_at="2026-11-10T00:00:00Z", cases=cases)


def test_a_new_suite_passes_and_fixes_are_listed():
    base = EvalRun(results=[result("a", 0, 1)])
    new = EvalRun(results=[result("a", 1, 1), result("b", 0, 0)])
    a, b = compare_runs(base, new)
    assert a.ok and [f.id for f in a.fixes] == ["c0"] and a.delta == 0.5
    assert b.ok and b.before == b.after == 0
    assert "| b | 0.0% | 0.0% | +0.0% | ok |" in to_markdown([a, b], 0)
