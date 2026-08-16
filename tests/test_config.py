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


# --- config file -----------------------------------------------------------


def write(tmp_path, text, name="gitboard.toml"):
    f = tmp_path / name
    f.write_text(text)
    return str(f)


def test_file_supplies_url(monkeypatch, tmp_path):
    monkeypatch.delenv("GITLAB_URL", raising=False)
    path = write(tmp_path, 'url = "http://from-file"\n')
    assert config.configure(config_path=path).url == "http://from-file"


def test_env_beats_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GITLAB_URL", "http://from-env")
    path = write(tmp_path, 'url = "http://from-file"\n')
    assert config.configure(config_path=path).url == "http://from-env"


def test_flag_beats_env_and_file(monkeypatch, tmp_path):
    monkeypatch.setenv("GITLAB_URL", "http://from-env")
    path = write(tmp_path, 'url = "http://from-file"\n')
    cfg = config.configure(url="http://from-flag", config_path=path)
    assert cfg.url == "http://from-flag"


def test_file_supplies_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("GITLAB_URL", raising=False)
    path = write(tmp_path, 'project = "grp/proj"\nspec = "b.yaml"\nboard = "Dev"\n')
    cfg = config.configure(config_path=path)
    assert (cfg.project, cfg.spec, cfg.board) == ("grp/proj", "b.yaml", "Dev")


def test_a_token_in_the_file_is_ignored_with_a_warning(monkeypatch, tmp_path):
    """Credentials belong in the keychain, not a file that can be committed."""
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="from-keychain\n"),
    )
    path = write(tmp_path, 'token = "glpat-oops"\n')
    cfg = config.configure(config_path=path)
    assert cfg.token() == "from-keychain"
    assert any("ignoring 'token'" in w for w in cfg.warnings)


def test_unknown_keys_warn_but_do_not_fail(monkeypatch, tmp_path):
    monkeypatch.delenv("GITLAB_URL", raising=False)
    path = write(tmp_path, 'url = "http://x"\nnope = 1\n')
    cfg = config.configure(config_path=path)
    assert cfg.url == "http://x"
    assert any("nope" in w for w in cfg.warnings)


def test_malformed_toml_is_an_error(tmp_path):
    path = write(tmp_path, "url = [[[\n")
    with pytest.raises(config.ConfigError):
        config.configure(config_path=path)


def test_a_missing_explicit_file_is_an_error(tmp_path):
    """Asking for a config that isn't there must not silently fall back."""
    with pytest.raises(config.ConfigError) as e:
        config.configure(config_path=str(tmp_path / "absent.toml"))
    assert "no such config file" in str(e.value)


def test_no_file_anywhere_is_fine(monkeypatch, tmp_path):
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.delenv("GITBOARD_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    cfg = config.get_config()
    assert cfg.source is None and cfg.url == config.DEFAULT_URL


def test_cwd_file_is_found_without_being_named(monkeypatch, tmp_path):
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.delenv("GITBOARD_CONFIG", raising=False)
    write(tmp_path, 'url = "http://cwd"\n')
    monkeypatch.chdir(tmp_path)
    assert config.get_config().url == "http://cwd"


def test_cwd_beats_user_config(monkeypatch, tmp_path):
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.delenv("GITBOARD_CONFIG", raising=False)
    xdg = tmp_path / "xdg" / "gitboard"
    xdg.mkdir(parents=True)
    (xdg / "config.toml").write_text('url = "http://user"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    write(tmp_path, 'url = "http://cwd"\n')
    monkeypatch.chdir(tmp_path)
    assert config.get_config().url == "http://cwd"
