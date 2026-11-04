from pathlib import Path

import pytest

from llmops.prompts import Prompt, PromptError, PromptRegistry, placeholders

EXAMPLES = Path(__file__).resolve().parent.parent / "prompts"


def write(folder, name, text):
    (folder / name).write_text(text)
    return folder


MINIMAL = """
name: greet
versions:
  - version: 1
    model: m1
    messages:
      - {role: user, content: "hi {name}"}
  - version: 2
    model: m2
    temperature: 0.2
    messages:
      - {role: system, content: "be warm, use {{curly}} braces literally"}
      - {role: user, content: "hello {name}"}
"""


def test_the_example_prompts_load():
    reg = PromptRegistry.from_dir(EXAMPLES)
    assert reg.names() == ["summarize-ticket", "support-reply"]
    support = reg["support-reply"]
    assert support.active_version.version == 2
    assert support.active_version.variables == {"product", "article", "question"}


def test_render_active_and_pinned_versions(tmp_path):
    reg = PromptRegistry.from_dir(write(tmp_path, "greet.yaml", MINIMAL))
    latest = reg.render("greet", {"name": "Aditya"})
    assert latest.model == "m2" and latest.temperature == 0.2
    assert [m.content for m in latest.messages] == ["be warm, use {curly} braces literally", "hello Aditya"]
    pinned = reg.render("greet", {"name": "Aditya"}, version=1)
    assert pinned.model == "m1" and pinned.messages[0].content == "hi Aditya"


def test_values_with_braces_are_not_templated(tmp_path):
    reg = PromptRegistry.from_dir(write(tmp_path, "greet.yaml", MINIMAL))
    msg = reg.render("greet", {"name": "{evil} {{x}}"}).messages[-1]
    assert msg.content == "hello {evil} {{x}}"


def test_missing_and_unknown_variables(tmp_path):
    reg = PromptRegistry.from_dir(write(tmp_path, "greet.yaml", MINIMAL))
    with pytest.raises(PromptError, match="missing value"):
        reg.render("greet")
    with pytest.raises(PromptError, match="unknown variable.*nmae"):
        reg.render("greet", {"name": "a", "nmae": "typo"})


def test_unknown_prompt_and_version(tmp_path):
    reg = PromptRegistry.from_dir(write(tmp_path, "greet.yaml", MINIMAL))
    with pytest.raises(PromptError, match="no prompt called 'nope'. known: greet"):
        reg["nope"]
    with pytest.raises(PromptError, match="no version 7"):
        reg["greet"].get(7)


V1 = "{version: 1, model: m, messages: [{role: user, content: x}]}"


@pytest.mark.parametrize(
    "text, message",
    [
        ("name: Bad Name\nversions: [{version: 1, model: m, messages: [{role: user, content: x}]}]", "name"),
        (
            "name: a\nactive: 3\nversions: [{version: 1, model: m, messages: [{role: user, content: x}]}]",
            "active",
        ),
        (
            "name: a\nversions: [{version: 2, model: m, messages: [{role: user, content: x}]},"
            " {version: 1, model: m, messages: [{role: user, content: x}]}]",
            "increasing order",
        ),
        (
            "name: a\nversions: [{version: 1, model: m, messages: [{role: user, content: '{x.y}'}]}]",
            "plain names",
        ),
        (
            "name: a\nversions: [{version: 1, model: m, messages: [{role: user, content: 'oops {'}]}]",
            "broken",
        ),
        ("name: [unclosed", "greet.yaml"),
    ],
)
def test_bad_files_fail_at_load_time_with_the_file_name(tmp_path, text, message):
    write(tmp_path, "greet.yaml", text)
    with pytest.raises(PromptError, match=message) as err:
        PromptRegistry.from_dir(tmp_path)
    assert "greet.yaml" in str(err.value)


def test_duplicate_names(tmp_path):
    write(tmp_path, "a.yaml", MINIMAL)
    write(tmp_path, "b.yml", MINIMAL)
    with pytest.raises(PromptError, match="defined twice"):
        PromptRegistry.from_dir(tmp_path)


def test_missing_folder(tmp_path):
    with pytest.raises(PromptError, match="doesn't exist"):
        PromptRegistry.from_dir(tmp_path / "nope")


def test_any_variable_name_works(tmp_path):
    text = (
        "name: v\nversions: [{version: 1, model: m, messages: [{role: user, content: '{version} {self}'}]}]"
    )
    reg = PromptRegistry.from_dir(write(tmp_path, "v.yaml", text))
    assert reg.render("v", {"version": "3", "self": "me"}).messages[0].content == "3 me"


def test_placeholders():
    assert placeholders("{a} and {b} but not {{c}}") == {"a", "b"}


def test_prompt_model_directly():
    p = Prompt(
        name="x", versions=[{"version": 1, "model": "m", "messages": [{"role": "user", "content": "{q}"}]}]
    )
    assert p.get().to_request(q="hi").messages[0].content == "hi"
