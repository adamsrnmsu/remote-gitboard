"""Tests for the config singleton and client.py's error mapping."""

import os
import types

import pytest

from gitboard import client, config


@pytest.fixture(autouse=True)
def clean_config(monkeypatch, tmp_path_factory):
    """Isolate every test from the developer's real environment.

    Three things leak in otherwise, and each one made a test lie: the
    singleton outlives a test unless reset; a .env is found by walking up from
    the cwd, so running pytest from the repo finds the repo's; and
    ~/.config/gitboard/config.toml is found via XDG. Point all of them
    somewhere empty.
    """
    empty = tmp_path_factory.mktemp("isolated")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(empty / "xdg"))
    monkeypatch.delenv("GITBOARD_CONFIG", raising=False)
    monkeypatch.chdir(empty)
    config.reset()
    yield
    config.reset()


# --- singleton -------------------------------------------------------------


def test_config_is_cached_until_configure_replaces_it():
    first = config.get_config()
    assert config.get_config() is first
    second = config.configure(url="http://elsewhere")
    assert second is not first
    assert config.get_config() is second


def test_reset_clears_overrides(monkeypatch):
    monkeypatch.delenv("GITLAB_URL", raising=False)
    config.configure(url="http://elsewhere")
    config.reset()
    assert config.get_config().url == config.DEFAULT_URL


# --- url resolution --------------------------------------------------------


# --- token resolution ------------------------------------------------------


def test_env_token_wins_over_keychain(monkeypatch):
    monkeypatch.setenv("GITLAB_READ_TOKEN", "from-env")
    monkeypatch.setattr(
        config.subprocess, "run", lambda *a, **k: pytest.fail("keychain was consulted")
    )
    assert config.get_config().token() == "from-env"


def test_keychain_is_the_fallback(monkeypatch):
    monkeypatch.delenv("GITLAB_READ_TOKEN", raising=False)
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="from-keychain\n"),
    )
    assert config.get_config().token() == "from-keychain"


def test_missing_token_raises_an_actionable_error(monkeypatch):
    monkeypatch.delenv("GITLAB_READ_TOKEN", raising=False)
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""),
    )
    with pytest.raises(config.ConfigError) as e:
        config.get_config().token()
    assert "GITLAB_READ_TOKEN" in str(e.value)


def test_token_is_lazy_and_resolved_once(monkeypatch):
    """`gitboard --help` must never hit the keychain."""
    monkeypatch.delenv("GITLAB_READ_TOKEN", raising=False)
    calls = []

    def once(*a, **k):
        calls.append(1)
        return types.SimpleNamespace(returncode=0, stdout="tok\n")

    monkeypatch.setattr(config.subprocess, "run", once)
    cfg = config.get_config()
    assert calls == [], "building the config alone must not resolve a token"
    cfg.token()
    cfg.token()
    assert len(calls) == 1, "token must be cached after the first lookup"


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


@pytest.mark.parametrize(
    "make_exc, want",
    [
        (lambda m: m.exceptions.GitlabGetError("404"), "no project 'grp/proj'"),
        (lambda m: m.exceptions.GitlabAuthenticationError("401"), "rejected the token"),
        (lambda m: ConnectionError("refused"), "cannot reach"),
    ],
    ids=["unknown-project", "rejected-token", "unreachable-host"],
)
def test_get_project_error_messages(monkeypatch, make_exc, want):
    assert want in run_get(monkeypatch, make_exc)


# --- config file -----------------------------------------------------------


def write(tmp_path, text, name="gitboard.toml"):
    f = tmp_path / name
    f.write_text(text)
    return str(f)


def test_file_supplies_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("GITLAB_URL", raising=False)
    path = write(tmp_path, 'project = "grp/proj"\nspec = "b.yaml"\nboard = "Dev"\n')
    cfg = config.configure(config_path=path)
    assert (cfg.project, cfg.board) == ("grp/proj", "Dev")
    assert cfg.spec == str(tmp_path.resolve() / "b.yaml")


def test_relative_spec_resolves_against_the_config_not_the_cwd(tmp_path):
    """Running from boards/ used to look for boards/boards/test.yaml."""
    path = write(tmp_path, 'spec = "boards/test.yaml"\n')
    sub = tmp_path / "boards"
    sub.mkdir()
    os.chdir(sub)
    config.reset()
    assert config.configure(config_path=path).spec == str(
        (tmp_path / "boards" / "test.yaml").resolve()
    )


def test_an_absolute_spec_is_left_alone(tmp_path):
    path = write(tmp_path, 'spec = "/somewhere/b.yaml"\n')
    assert config.configure(config_path=path).spec == "/somewhere/b.yaml"


def test_a_token_in_the_file_is_ignored_with_a_warning(monkeypatch, tmp_path):
    """Credentials belong in the keychain, not a file that can be committed."""
    monkeypatch.delenv("GITLAB_READ_TOKEN", raising=False)
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


def test_no_file_anywhere_is_fine():
    cfg = config.get_config()
    assert (cfg.source, cfg.env_source) == (None, None)
    assert cfg.url == config.DEFAULT_URL


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


# --- .env ------------------------------------------------------------------
# `.env` is gitignored and already exists for docker compose, so unlike
# gitboard.toml it is an acceptable home for a token.


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """A .env in a cwd with no gitboard.toml and no inherited env."""
    monkeypatch.delenv("GITLAB_URL", raising=False)
    monkeypatch.delenv("GITLAB_READ_TOKEN", raising=False)
    monkeypatch.delenv("GITBOARD_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)

    def write(text):
        (tmp_path / ".env").write_text(text)
        config.reset()

    return write


# One row per layer: flag > env > .env > gitboard.toml > default. The files sit
# in the parent of the cwd, so the upward walk is exercised on every row.
@pytest.mark.parametrize(
    "flag, env, dotenv, toml, want",
    [
        (None, None, None, None, "https://gitlab.com"),
        (None, None, None, "toml", "http://from-toml"),
        (None, None, "dotenv", "toml", "http://from-dotenv"),
        (None, "env", "dotenv", "toml", "http://from-env"),
        ("flag", "env", "dotenv", "toml", "http://from-flag"),
        (None, None, "", "toml", "http://from-toml"),
    ],
    ids=["default", "toml", "dotenv>toml", "env>dotenv", "flag>env", "empty-dotenv"],
)
def test_url_precedence(env_file, monkeypatch, tmp_path, flag, env, dotenv, toml, want):
    if toml:
        (tmp_path / "gitboard.toml").write_text(f'url = "http://from-{toml}"\n')
    if dotenv is not None:
        env_file(f"GITLAB_URL={'http://from-' + dotenv if dotenv else ''}\n")
    if env:
        monkeypatch.setenv("GITLAB_URL", f"http://from-{env}")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    config.reset()
    url = config.configure(url=f"http://from-{flag}" if flag else None).url
    assert url == want


def test_env_file_supplies_token(env_file, monkeypatch):
    monkeypatch.setattr(
        config.subprocess, "run", lambda *a, **k: pytest.fail("keychain was consulted")
    )
    env_file("GITLAB_READ_TOKEN=glpat-from-dotenv\n")
    cfg = config.get_config()
    assert cfg.token() == "glpat-from-dotenv"
    assert cfg.token_source.endswith(".env")


@pytest.mark.parametrize(
    "text",
    [
        'GITLAB_URL="http://x"\n',
        "# a comment\n\nGITLAB_URL=http://x\n",
        "GITLAB_ROOT_PASSWORD=hunter2\nGITLAB_URL=http://x\n",
    ],
    ids=["quoted", "comments-and-blanks", "unrelated-keys-ignored"],
)
def test_env_file_parsing(env_file, text):
    """.env is shared with docker compose: quotes are not the value, its other
    keys must not leak in."""
    env_file(text)
    cfg = config.get_config()
    assert cfg.url == "http://x"
    assert "hunter2" not in repr(cfg)


# --- write token -----------------------------------------------------------
# Two slots because the scopes differ: reading wants read_api, writing needs
# api. Keeping them apart is what lets the read-only AI pass stay read-only on
# a machine that is also able to write.


def test_write_token_is_separate_from_the_read_token(monkeypatch):
    monkeypatch.setenv("GITLAB_READ_TOKEN", "read-tok")
    monkeypatch.setenv("GITLAB_WRITE_TOKEN", "write-tok")
    cfg = config.get_config()
    assert cfg.token() == "read-tok"
    assert cfg.token(write=True) == "write-tok"


def test_write_falls_back_to_the_read_token(monkeypatch):
    """A single api-scope token is a legitimate setup."""
    monkeypatch.setenv("GITLAB_READ_TOKEN", "only-tok")
    monkeypatch.delenv("GITLAB_WRITE_TOKEN", raising=False)
    monkeypatch.setattr(
        config.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(returncode=1, stdout=""),
    )
    cfg = config.get_config()
    assert cfg.token(write=True) == "only-tok"
    assert cfg.write_token_source is None


def test_write_token_from_env_file(env_file):
    env_file("GITLAB_READ_TOKEN=read-tok\nGITLAB_WRITE_TOKEN=write-tok\n")
    cfg = config.get_config()
    assert cfg.token(write=True) == "write-tok"
    assert cfg.write_token_source.endswith(".env")


def test_write_token_flag_beats_env(monkeypatch):
    monkeypatch.setenv("GITLAB_WRITE_TOKEN", "from-env")
    assert config.configure(write_token="from-flag").token(write=True) == "from-flag"


def test_write_keychain_is_consulted_before_falling_back(monkeypatch):
    monkeypatch.setenv("GITLAB_READ_TOKEN", "read-tok")
    monkeypatch.delenv("GITLAB_WRITE_TOKEN", raising=False)
    asked = []

    def keychain(cmd, **k):
        asked.append(cmd[cmd.index("-s") + 1])
        return types.SimpleNamespace(returncode=0, stdout="from-write-keychain\n")

    monkeypatch.setattr(config.subprocess, "run", keychain)
    assert config.get_config().token(write=True) == "from-write-keychain"
    assert asked == [config.WRITE_KEYCHAIN_SERVICE]


@pytest.mark.parametrize(
    "code, scoped", [(403, True), (500, False)], ids=["403", "500"]
)
def test_write_errors_name_the_scope_only_for_a_403(monkeypatch, code, scoped):
    """The 403 a read_api token gets must name the fix; a 500 must not send the
    user chasing tokens."""
    mod = fake_gitlab_module(None)

    class GitlabError(Exception):
        def __init__(self, code):
            self.response_code = code
            super().__init__("boom")

    mod.exceptions.GitlabError = GitlabError
    monkeypatch.setitem(__import__("sys").modules, "gitlab", mod)

    with pytest.raises(client.GitlabProblem) as e, client.write_errors():
        raise GitlabError(code)
    msg = str(e.value)
    assert ("api` scope" in msg and "GITLAB_WRITE_TOKEN" in msg) is scoped
    assert ("scope" in msg) is scoped


# --- the GITLAB_TOKEN -> GITLAB_READ_TOKEN rename --------------------------


def test_legacy_env_var_still_works_and_warns(monkeypatch):
    """An export sitting in a shell must not silently fall through to the
    keychain and read the wrong instance."""
    monkeypatch.delenv("GITLAB_READ_TOKEN", raising=False)
    monkeypatch.setenv("GITLAB_TOKEN", "legacy-tok")
    monkeypatch.setattr(
        config.subprocess, "run", lambda *a, **k: pytest.fail("keychain was consulted")
    )
    cfg = config.get_config()
    assert cfg.token() == "legacy-tok"
    assert any("deprecated" in w for w in cfg.warnings)
    assert "deprecated" in cfg.token_source


def test_new_env_var_beats_legacy(monkeypatch):
    monkeypatch.setenv("GITLAB_READ_TOKEN", "new-tok")
    monkeypatch.setenv("GITLAB_TOKEN", "legacy-tok")
    cfg = config.get_config()
    assert cfg.token() == "new-tok"
    assert not any("deprecated" in w for w in cfg.warnings)


def test_new_keychain_service_is_preferred(monkeypatch):
    monkeypatch.delenv("GITLAB_READ_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    asked = []

    def keychain(cmd, **k):
        service = cmd[cmd.index("-s") + 1]
        asked.append(service)
        ok = service == config.READ_KEYCHAIN_SERVICE
        return types.SimpleNamespace(returncode=0 if ok else 1, stdout="new\n")

    monkeypatch.setattr(config.subprocess, "run", keychain)
    assert config.get_config().token() == "new"
    assert asked == [config.READ_KEYCHAIN_SERVICE]


def test_legacy_keychain_service_is_the_fallback(monkeypatch):
    """A keychain item created before the rename must keep working."""
    monkeypatch.delenv("GITLAB_READ_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    asked = []

    def keychain(cmd, **k):
        service = cmd[cmd.index("-s") + 1]
        asked.append(service)
        ok = service == config.LEGACY_KEYCHAIN_SERVICE
        return types.SimpleNamespace(returncode=0 if ok else 1, stdout="legacy\n")

    monkeypatch.setattr(config.subprocess, "run", keychain)
    assert config.get_config().token() == "legacy"
    assert asked == [config.READ_KEYCHAIN_SERVICE, config.LEGACY_KEYCHAIN_SERVICE]


def test_no_security_binary_is_just_no_keychain(monkeypatch):
    """Linux, a container: `security` does not exist. That is a missing
    token, not a traceback."""
    monkeypatch.delenv("GITLAB_READ_TOKEN", raising=False)

    def no_security(*a, **k):
        raise FileNotFoundError("security")

    monkeypatch.setattr(config.subprocess, "run", no_security)
    with pytest.raises(config.ConfigError) as e:
        config.get_config().token()
    assert "GITLAB_READ_TOKEN" in str(e.value)


def test_guide_is_on_by_default_and_toml_or_env_turn_it_off(tmp_path, monkeypatch):
    assert config.get_config().guide is True
    (tmp_path / "gitboard.toml").write_text("guide = false\n")
    monkeypatch.chdir(tmp_path)
    config.reset()
    assert config.get_config().guide is False
    for value, want in (("1", True), ("0", False), ("off", False), ("False", False)):
        monkeypatch.setenv("GITBOARD_GUIDE", value)  # env beats the toml
        config.reset()
        assert config.get_config().guide is want
