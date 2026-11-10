from pathlib import Path

import pytest

from llmops.evals import EvalSuite, SuiteError, available_scorers, run_suite
from llmops.evals.scorers import get_scorer
from llmops.prompts import PromptRegistry
from llmops.providers import CompletionRequest, CompletionResponse
from llmops.providers.fake import failing

pytestmark = pytest.mark.anyio
PROMPTS = PromptRegistry.from_dir(Path(__file__).resolve().parent.parent / "prompts")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def check(type_, output, **params):
    scorer = get_scorer(type_)
    return scorer.score(output, scorer.Params.model_validate(params))


@pytest.mark.parametrize(
    "type_, params, output, passed",
    [
        ("contains", {"value": "settings"}, "Open Settings > Account", True),
        ("contains", {"value": "settings", "ignore_case": False}, "Open Settings", False),
        ("not_contains", {"value": "as an AI"}, "As an AI, I can't", False),
        ("contains_any", {"values": ["human colleague", "pass this on"]}, "I'll pass this on.", True),
        ("contains_any", {"values": ["refund"]}, "No idea.", False),
        ("regex", {"pattern": r"\$\d+", "must_match": False}, "It costs $40 a month", False),
        ("regex", {"pattern": r"\$\d+", "must_match": False}, "See the pricing page", True),
        ("regex", {"pattern": r"^Hi"}, "Hi there", True),
        ("max_words", {"max_words": 3}, "one two three four", False),
        ("json", {"required_keys": ["summary"]}, '{"summary": "x"}', True),
        ("json", {"required_keys": ["summary"]}, '```json\n{"summary": "x"}\n```', True),
        ("json", {"required_keys": ["summary"]}, '{"other": 1}', False),
        ("json", {}, "not json", False),
    ],
)
def test_scorers(type_, params, output, passed):
    result = check(type_, output, **params)
    assert result.passed is passed
    assert bool(result.detail) is not passed


def test_builtin_scorers_are_registered():
    assert {"contains", "not_contains", "contains_any", "regex", "max_words", "json"} <= set(
        available_scorers()
    )


SUITE = {
    "prompt": "support-reply",
    "checks": [{"type": "not_contains", "value": "as an AI"}],
    "cases": [
        {
            "id": "reset",
            "variables": {"product": "Acme", "article": "Go to Settings.", "question": "Reset?"},
            "checks": [{"type": "contains", "value": "settings"}],
        },
        {
            "id": "pricing",
            "variables": {"product": "Acme", "article": "Go to Settings.", "question": "Price?"},
            "checks": [{"type": "regex", "pattern": r"\$\d+", "must_match": False}],
        },
    ],
}


@pytest.mark.parametrize(
    "change, message",
    [
        ({"cases": []}, "at least 1"),
        ({"checks": [{"type": "sounds_nice"}]}, "unknown check type"),
        ({"checks": [{"type": "contains"}]}, "value"),
        ({"checks": [{"type": "contains", "value": "x", "typo": 1}]}, "typo"),
        ({"checks": [], "cases": [{"id": "a", "variables": {}}]}, "no checks"),
        ({"cases": [SUITE["cases"][0], SUITE["cases"][0]]}, "duplicate case ids: reset"),
    ],
)
def test_bad_suites(change, message):
    with pytest.raises(ValueError, match=message):
        EvalSuite.model_validate({**SUITE, **change})


def test_load_names_the_file(tmp_path):
    path = tmp_path / "broken.yaml"
    path.write_text("prompt: x\ncases: []\n")
    with pytest.raises(SuiteError, match="broken.yaml"):
        EvalSuite.load(path)


def scripted(answers):
    """A `complete` function answering from a dict keyed by the question, recording requests."""
    seen: list[CompletionRequest] = []

    async def complete(model: str, request: CompletionRequest) -> CompletionResponse:
        seen.append(request)
        answer = answers[request.messages[-1].content]
        if isinstance(answer, Exception):
            raise answer
        return CompletionResponse(text=answer, model=model, provider="fake")

    complete.seen = seen  # type: ignore[attr-defined]
    return complete


async def test_run_scores_each_case():
    complete = scripted({"Reset?": "Open Settings and click Reset.", "Price?": "It's $10 a month."})
    result = await run_suite(EvalSuite.model_validate(SUITE), PROMPTS, complete)
    assert (result.prompt, result.version, result.model) == ("support-reply", 2, "gpt-5-mini")
    reset, pricing = result.cases
    assert reset.passed and reset.score == 1
    assert not pricing.passed and pricing.score == 0.5
    assert pricing.checks[1].detail == "matched '$10'"
    assert result.score == 0.75 and result.pass_rate == 0.5
    # the article made it into the prompt
    assert "Go to Settings." in complete.seen[0].messages[0].content


async def test_older_versions_ignore_variables_they_dont_use():
    complete = scripted({"Reset?": "settings", "Price?": "ask sales"})
    result = await run_suite(EvalSuite.model_validate(SUITE), PROMPTS, complete, version=1, model="echo")
    assert result.version == 1 and result.model == "echo" and result.pass_rate == 1
    assert "Article" not in complete.seen[0].messages[0].content
    assert complete.seen[0].model == "echo"


async def test_typos_in_variables_still_fail():
    suite = {
        **SUITE,
        "cases": [{**SUITE["cases"][0], "variables": {"prodcut": "Acme", "question": "Reset?"}}],
    }
    result = await run_suite(EvalSuite.model_validate(suite), PROMPTS, scripted({}))
    assert result.cases[0].error.startswith("prompt:") and result.cases[0].score == 0


async def test_model_errors_score_zero_but_dont_stop_the_run():
    complete = scripted({"Reset?": failing(503)[0], "Price?": "no prices here"})
    result = await run_suite(EvalSuite.model_validate(SUITE), PROMPTS, complete)
    assert result.cases[0].error.startswith("model:") and not result.cases[0].passed
    assert result.cases[1].passed and result.score == 0.5


async def test_results_serialize_with_scores():
    complete = scripted({"Reset?": "settings", "Price?": "$5"})
    data = (await run_suite(EvalSuite.model_validate(SUITE), PROMPTS, complete)).model_dump(mode="json")
    assert data["score"] == 0.75 and data["cases"][1]["score"] == 0.5 and data["cases"][0]["passed"] is True
