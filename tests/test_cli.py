from pathlib import Path

import pytest

from llmops import __version__, cli


def test_version(capsys):
    cli.main(["version"])
    assert capsys.readouterr().out.strip() == __version__


def test_serve_passes_settings_to_uvicorn(monkeypatch):
    seen = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: seen.update(app=app, **kw))
    cli.main(["serve", "--port", "9001"])
    assert seen["app"] == "llmops.app:create_app" and seen["factory"] is True
    assert seen["port"] == 9001 and seen["host"] == "127.0.0.1"


def test_command_is_required():
    with pytest.raises(SystemExit):
        cli.main([])


EXAMPLES = str(Path(__file__).resolve().parent.parent / "prompts")


def run_prompts(capsys, *args):
    with pytest.raises(SystemExit) as code:
        cli.main(["prompts", "--dir", EXAMPLES, *args])
    out = capsys.readouterr()
    return code.value.code, out.out, out.err


def test_prompts_list(capsys):
    code, out, _ = run_prompts(capsys, "list")
    assert code == 0
    assert "support-reply" in out and "v1, v2*" in out and "summarize-ticket" in out


def test_prompts_show_active_and_pinned(capsys):
    _, out, _ = run_prompts(capsys, "show", "support-reply")
    assert out.startswith("# support-reply v2") and "variables: article, product, question" in out
    _, out, _ = run_prompts(capsys, "show", "support-reply", "--version", "1")
    assert "# support-reply v1" in out and "article" not in out


def test_prompts_diff(capsys):
    code, out, _ = run_prompts(capsys, "diff", "support-reply", "1", "2")
    assert code == 0
    assert "--- support-reply v1" in out and "+++ support-reply v2" in out
    assert "Never invent features or prices." in out and "+    {article}" in out


def test_prompts_check_and_errors(capsys, tmp_path):
    assert run_prompts(capsys, "check")[0] == 0
    code, _, err = run_prompts(capsys, "show", "nope")
    assert code == 1 and "no prompt called 'nope'" in err

    (tmp_path / "bad.yaml").write_text("name: bad\nversions: []\n")
    with pytest.raises(SystemExit) as exit_code:
        cli.main(["prompts", "--dir", str(tmp_path), "check"])
    assert exit_code.value.code == 1
    assert "bad.yaml" in capsys.readouterr().err
