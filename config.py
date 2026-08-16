"""Process-wide configuration. One instance, built on first use.

The singleton is `get_config()` — an lru_cache of size 1, which is the
idiomatic Python singleton. The CLI calls `configure()` once with whatever
came off the command line; everything downstream just calls `get_config()`
and never has to thread arguments through.

Precedence, highest first:

    --flag  >  environment  >  .env file  >  config file  >  built-in default

`.env` is just environment variables that live in a file, so it sits directly
below the real environment: a real export always wins over it. It is the one
place a token may be stored, because it is gitignored and already exists for
docker compose. `gitboard.toml` still refuses tokens — it is meant to be
shared, `.env` is not.

The config file is TOML, read with stdlib tomllib — no dependency. Searched
in this order, first hit wins:

    1. --config PATH, or $GITBOARD_CONFIG
    2. ./gitboard.toml
    3. ~/.config/gitboard/config.toml  ($XDG_CONFIG_HOME honoured)

Tokens deliberately do not come from `gitboard.toml`. A `token` key there is
ignored with a warning — put it in `.env` or the keychain instead.

Token lookup stays lazy: `Config.token()` is only resolved when something
actually needs to talk to GitLab, so `gitboard --help` never touches the
keychain.
"""

import os
import subprocess
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_URL = "https://gitlab.com"
READ_KEYCHAIN_SERVICE = "gitlab-read-token"
WRITE_KEYCHAIN_SERVICE = "gitlab-write-token"
LEGACY_KEYCHAIN_SERVICE = "gitlab-token"  # pre-rename name, still honoured
LEGACY_ENV_VAR = "GITLAB_TOKEN"  # ditto — warned about, not broken
FILENAME = "gitboard.toml"
ENV_FILENAME = ".env"
HERE = Path(__file__).resolve().parent
KNOWN_KEYS = {"url", "project", "board", "spec"}

_overrides: dict[str, object] = {}


class ConfigError(Exception):
    """Config is unusable — no token, bad file. The CLI renders this."""


@dataclass
class Config:
    url: str
    verbose: bool = False
    project: str | None = None  # default for `show`
    board: str | None = None  # default board name
    spec: str | None = None  # default for `plan` / `apply`
    source: Path | None = None  # which toml file these came from, if any
    env_source: Path | None = None  # which .env file was read, if any
    warnings: tuple[str, ...] = ()
    token_source: str = "keychain"  # which source supplied the read token
    write_token_source: str | None = None  # None => falls back to the read one
    token_override: str | None = None
    write_token_override: str | None = None
    _token: str | None = field(default=None, repr=False)
    _write_token: str | None = field(default=None, repr=False)

    def token(self, write: bool = False) -> str:
        """The read token, or the write one for `apply`.

        Two slots because the scopes differ: reading wants `read_api`, writing
        needs `api`. Keeping them apart is what lets the read-only AI pass stay
        read-only even on a machine that can write.

        With no write token configured this falls back to the read token — a
        single `api`-scope token is a legitimate setup — and GitLab refuses the
        write if that token lacks the scope. client.write_errors() explains it.
        """
        if not write:
            if self._token is None:
                self._token = self.token_override or _from_keychain(
                    READ_KEYCHAIN_SERVICE, also=LEGACY_KEYCHAIN_SERVICE
                )
            return self._token

        if self._write_token is None:
            self._write_token = self.write_token_override or _from_keychain(
                WRITE_KEYCHAIN_SERVICE, fallback=self.token
            )
        return self._write_token


def _keychain_lookup(service):
    out = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-w"],
        capture_output=True,
        text=True,
    )
    return None if out.returncode else out.stdout.strip()


def _from_keychain(service, also=None, fallback=None, env_var=None) -> str:
    """`also` is a second service name to try — the pre-rename one."""
    for name in (service, also):
        if name and (found := _keychain_lookup(name)):
            return found
    if fallback is not None:
        return fallback()  # no dedicated write token; reuse the read one
    add = f'security add-generic-password -a "$USER" -s {service} -w'
    raise ConfigError(
        f"no token — set {env_var or 'GITLAB_READ_TOKEN'}, pass the matching "
        f"flag, or add a keychain item:\n  {add} '<PAT>'"
    )


def env_file_paths():
    """cwd first, then next to the scripts — so running from elsewhere works."""
    seen, paths = set(), []
    for path in (Path.cwd() / ENV_FILENAME, HERE / ENV_FILENAME):
        if path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


@lru_cache(maxsize=1)
def load_env_file():
    """(values, path). Never touches os.environ — precedence stays explicit."""
    for path in env_file_paths():
        if path.is_file():
            values = {k: v for k, v in dotenv_values(path).items() if v}
            return values, path
    return {}, None


def candidate_paths(explicit=None):
    """Where a config file may live, highest priority first."""
    if explicit := explicit or os.environ.get("GITBOARD_CONFIG"):
        return [Path(explicit).expanduser()]
    xdg = os.environ.get("XDG_CONFIG_HOME")
    user_dir = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return [Path.cwd() / FILENAME, user_dir / "gitboard" / "config.toml"]


def load_file(explicit=None):
    """(values, path, warnings). Missing files are fine; malformed ones are not.

    An explicitly requested file that does not exist *is* an error — asking for
    a config that isn't there should not silently fall back.
    """
    warnings = []
    for path in candidate_paths(explicit):
        if not path.is_file():
            continue
        try:
            with open(path, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e

        if "token" in data:
            warnings.append(
                f"{path}: ignoring 'token' — keep credentials in the keychain, "
                "GITLAB_TOKEN, or --token, not a file that can be committed"
            )
        if unknown := set(data) - KNOWN_KEYS - {"token"}:
            warnings.append(f"{path}: ignoring unknown key(s) {sorted(unknown)}")

        return {k: v for k, v in data.items() if k in KNOWN_KEYS}, path, warnings

    if explicit or os.environ.get("GITBOARD_CONFIG"):
        raise ConfigError(f"no such config file: {candidate_paths(explicit)[0]}")
    return {}, None, warnings


def configure(
    *, url=None, token=None, write_token=None, verbose=None, config_path=None
) -> "Config":
    """Apply CLI overrides. Call once, before anything reads config."""
    for key, value in (
        ("url", url),
        ("token", token),
        ("write_token", write_token),
        ("verbose", verbose),
        ("config_path", config_path),
    ):
        if value is not None:
            _overrides[key] = value
    get_config.cache_clear()
    return get_config()


@lru_cache(maxsize=1)
def get_config() -> Config:
    values, source, warnings = load_file(_overrides.get("config_path"))
    env_values, env_source = load_env_file()

    def pick(key, env_var=None, default=None):
        """flag > environment > .env > gitboard.toml > default."""
        if (override := _overrides.get(key)) is not None:
            return override
        if env_var:
            if env := os.environ.get(env_var):
                return env
            if env := env_values.get(env_var):
                return env
        return values.get(key, default)

    if _overrides.get("write_token"):
        write_token_source = "--write-token"
    elif os.environ.get("GITLAB_WRITE_TOKEN"):
        write_token_source = "environment"
    elif env_values.get("GITLAB_WRITE_TOKEN"):
        write_token_source = str(env_source)
    else:
        write_token_source = None  # falls back to the read token

    if _overrides.get("token"):
        token_source = "--read-token"
    elif os.environ.get("GITLAB_READ_TOKEN"):
        token_source = "environment"
    elif env_values.get("GITLAB_READ_TOKEN"):
        token_source = str(env_source)
    elif os.environ.get(LEGACY_ENV_VAR) or env_values.get(LEGACY_ENV_VAR):
        token_source = f"{LEGACY_ENV_VAR} (deprecated)"
        warnings.append(
            f"{LEGACY_ENV_VAR} is deprecated — rename it to GITLAB_READ_TOKEN "
            "so it reads clearly next to GITLAB_WRITE_TOKEN"
        )
    else:
        token_source = "keychain"

    return Config(
        url=pick("url", "GITLAB_URL", DEFAULT_URL),
        verbose=bool(_overrides.get("verbose", False)),
        project=pick("project"),
        board=pick("board"),
        spec=pick("spec"),
        source=source,
        env_source=env_source,
        warnings=tuple(warnings),
        token_source=token_source,
        write_token_source=write_token_source,
        token_override=pick("token", "GITLAB_READ_TOKEN")
        or pick("token", LEGACY_ENV_VAR),
        write_token_override=pick("write_token", "GITLAB_WRITE_TOKEN"),
    )


def reset() -> None:
    """Drop overrides and the cached instances. For tests."""
    _overrides.clear()
    get_config.cache_clear()
    load_env_file.cache_clear()
