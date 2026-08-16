"""Tests for the config singleton and client.py's error mapping."""

import types

import pytest

import client
import config


@pytest.fixture(autouse=True)
def clean_config():
    """The singleton outlives a test unless it's reset. Do it on both sides."""
    config.reset()
    yield
    config.reset()


# --- singleton -------------------------------------------------------------


def test_get_config_returns_the_same_instance():
    assert config.get_config() is config.get_config()


def test_configure_replaces_the_cached_instance():
    first = config.get_config()
    second = config.configure(url="http://elsewhere")
    assert second is not first
    assert config.get_config() is second


def test_reset_clears_overrides(monkeypatch):
    monkeypatch.delenv("GITLAB_URL", raising=False)
    config.configure(url="http://elsewhere")
    config.reset()
    assert config.get_config().url == config.DEFAULT_URL


# --- url resolution --------------------------------------------------------


def test_flag_beats_env(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "http://from-env")
    assert config.configure(url="http://from-flag").url == "http://from-flag"


def test_env_beats_default(monkeypatch):
    monkeypatch.setenv("GITLAB_URL", "http://from-env")
    assert config.get_config().url == "http://from-env"


def test_default_is_gitlab_com(monkeypatch):
    monkeypatch.delenv("GITLAB_URL", raising=False)
    assert config.get_config().url == "https://gitlab.com"


# --- token resolution ------------------------------------------------------


def test_env_token_wins_over_keychain(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "from-env")
    monkeypatch.setattr(
        config.subprocess, "run", lambda *a, **k: pytest.fail("keychain was consulted")
    )
    assert config.get_config().token() == "from-env"


def test_keychain_is_the_fallback(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="from-keychain\n"),
    )
    assert config.get_config().token() == "from-keychain"


def test_missing_token_raises_an_actionable_error(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""),
    )
    with pytest.raises(config.ConfigError) as e:
        config.get_config().token()
    assert "GITLAB_TOKEN" in str(e.value)


def test_token_is_not_read_until_asked(monkeypatch):
    """`gitboard --help` must never hit the keychain."""
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(
        config.subprocess, "run", lambda *a, **k: pytest.fail("keychain was consulted")
    )
    config.get_config()  # building the config alone must not resolve a token


def test_token_is_resolved_once(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    calls = []

    def once(*a, **k):
        calls.append(1)
        return types.SimpleNamespace(returncode=0, stdout="tok\n")

    monkeypatch.setattr(config.subprocess, "run", once)
    cfg = config.get_config()
    cfg.token()
    cfg.token()
    assert len(calls) == 1


# --- client error mapping --------------------------------------------------


def fake_gitlab_module(exc):
    """A stand-in for the `gitlab` package, raising `exc` from projects.get."""
    mod = types.ModuleType("gitlab")

    class GitlabAuthenticationError(Exception):
        pass

    class GitlabGetError(Exception):
        pass

    mod.exceptions = types.SimpleNamespace(
        GitlabAuthenticationError=GitlabAuthenticationError,
        GitlabGetError=GitlabGetError,
    )
    return mod


def run_get(monkeypatch, exc_factory):
    mod = fake_gitlab_module(None)
    monkeypatch.setitem(__import__("sys").modules, "gitlab", mod)
    exc = exc_factory(mod)

    def get(_path):
        raise exc

    gl = types.SimpleNamespace(projects=types.SimpleNamespace(get=get))
    with pytest.raises(client.GitlabProblem) as e:
        client.get_project(gl, "grp/proj")
    return str(e.value)


def test_unknown_project_message(monkeypatch):
    msg = run_get(monkeypatch, lambda m: m.exceptions.GitlabGetError("404"))
    assert "no project 'grp/proj'" in msg


def test_rejected_token_message(monkeypatch):
    msg = run_get(monkeypatch, lambda m: m.exceptions.GitlabAuthenticationError("401"))
    assert "rejected the token" in msg


def test_unreachable_host_message(monkeypatch):
    msg = run_get(monkeypatch, lambda m: ConnectionError("refused"))
    assert "cannot reach" in msg
