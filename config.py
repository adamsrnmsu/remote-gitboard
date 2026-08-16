"""Process-wide configuration. One instance, built on first use.

The singleton is `get_config()` — an lru_cache of size 1, which is the
idiomatic Python singleton. The CLI calls `configure()` once with whatever
came off the command line; everything downstream just calls `get_config()`
and never has to thread arguments through.

Precedence, highest first:

    --flag  >  environment  >  config file  >  built-in default

The config file is TOML, read with stdlib tomllib — no dependency. Searched
in this order, first hit wins:

    1. --config PATH, or $GITBOARD_CONFIG
    2. ./gitboard.toml
    3. ~/.config/gitboard/config.toml  ($XDG_CONFIG_HOME honoured)

Tokens deliberately do not come from the file. The keychain is a better place
for a credential than a dotfile you might commit, so a `token` key is ignored
with a warning rather than honoured.

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

DEFAULT_URL = "https://gitlab.com"
KEYCHAIN_SERVICE = "gitlab-token"
FILENAME = "gitboard.toml"
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
    source: Path | None = None  # which file these came from, if any
    warnings: tuple[str, ...] = ()
    token_override: str | None = None
    _token: str | None = field(default=None, repr=False)

    def token(self) -> str:
        """Flag/env first, then the macOS keychain. Cached per instance."""
        if self._token is None:
            self._token = self.token_override or _from_keychain()
        return self._token


def _from_keychain() -> str:
    out = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True,
        text=True,
    )
    if out.returncode:
        add = f'security add-generic-password -a "$USER" -s {KEYCHAIN_SERVICE} -w'
        raise ConfigError(
            "no token — set GITLAB_TOKEN, pass --token, or add a keychain item:\n"
            f"  {add} '<PAT>'"
        )
    return out.stdout.strip()


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


def configure(*, url=None, token=None, verbose=None, config_path=None) -> "Config":
    """Apply CLI overrides. Call once, before anything reads config."""
    for key, value in (
        ("url", url),
        ("token", token),
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

    def pick(key, env_var=None, default=None):
        """flag > env > file > default."""
        if (override := _overrides.get(key)) is not None:
            return override
        if env_var and (env := os.environ.get(env_var)):
            return env
        return values.get(key, default)

    return Config(
        url=pick("url", "GITLAB_URL", DEFAULT_URL),
        verbose=bool(_overrides.get("verbose", False)),
        project=pick("project"),
        board=pick("board"),
        spec=pick("spec"),
        source=source,
        warnings=tuple(warnings),
        token_override=_overrides.get("token") or os.environ.get("GITLAB_TOKEN"),
    )


def reset() -> None:
    """Drop overrides and the cached instance. For tests."""
    _overrides.clear()
    get_config.cache_clear()
