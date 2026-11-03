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
