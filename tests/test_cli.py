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
    assert seen["app"] == "llmops.app:serve_app" and seen["factory"] is True
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


def test_keys_new_prints_a_key_and_its_hash(capsys):
    from llmops.auth import hash_key

    cli.main(["keys", "new", "search"])
    out = capsys.readouterr().out
    key = out.split("key for search: ")[1].split()[0]
    assert key.startswith("llmops-")
    assert f"key_sha256: {hash_key(key)}" in out


def test_usage_report(tmp_path, capsys):
    from datetime import datetime, timezone

    from llmops.usage import SQLiteUsageStore, UsageRecord

    db = tmp_path / "usage.db"
    store = SQLiteUsageStore(db)
    for team, model, cost in [
        ("search", "gpt-5-mini", 0.5),
        ("search", "gpt-5-mini", 0.25),
        ("ops", "echo", 0),
    ]:
        store.record(
            UsageRecord(
                ts=datetime(2026, 11, 3, tzinfo=timezone.utc),
                team=team,
                model=model,
                provider="p",
                prompt_tokens=100,
                completion_tokens=40,
                cost_usd=cost,
            )
        )
    store.close()

    with pytest.raises(SystemExit) as code:
        cli.main(["usage", "--db", str(db), "--month", "2026-11"])
    out = capsys.readouterr().out
    assert code.value.code == 0
    lines = out.splitlines()
    search = next(line for line in lines if line.startswith("search"))
    assert search.split() == ["search", "gpt-5-mini", "2", "200", "80", "0.7500"]
    assert next(line for line in lines if line.startswith("total")).split() == ["total", "3", "0.7500"]

    with pytest.raises(SystemExit) as code:
        cli.main(["usage", "--db", str(db), "--month", "2026-10", "--team", "search"])
    lines = capsys.readouterr().out.splitlines()
    assert code.value.code == 0 and not any(line.startswith("search") for line in lines)


def test_usage_report_errors(tmp_path, capsys):
    with pytest.raises(SystemExit) as code:
        cli.main(["usage", "--db", str(tmp_path / "missing.db")])
    assert code.value.code == 1 and "no usage database" in capsys.readouterr().err
