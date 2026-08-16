"""Process-wide configuration. One instance, built on first use.

The singleton is `get_config()` — an lru_cache of size 1, which is the
idiomatic Python singleton. The CLI calls `configure()` once with whatever
came off the command line; everything downstream just calls `get_config()`
and never has to thread arguments through.

Token lookup stays lazy: `Config.token()` is only resolved when something
actually needs to talk to GitLab, so `gitboard --help` never touches the
keychain.
"""

import os
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache

DEFAULT_URL = "https://gitlab.com"
KEYCHAIN_SERVICE = "gitlab-token"

_overrides: dict[str, object] = {}


class ConfigError(Exception):
    """Config is unusable — no token, typically. The CLI renders this."""


@dataclass
class Config:
    url: str
    verbose: bool = False
    token_override: str | None = None
    _token: str | None = field(default=None, repr=False)

    def token(self) -> str:
        """Env/flag first, then the macOS keychain. Cached per instance."""
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


def configure(*, url=None, token=None, verbose=None) -> "Config":
    """Apply CLI overrides. Call once, before anything reads config."""
    for key, value in (("url", url), ("token", token), ("verbose", verbose)):
        if value is not None:
            _overrides[key] = value
    get_config.cache_clear()
    return get_config()


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config(
        url=_overrides.get("url") or os.environ.get("GITLAB_URL", DEFAULT_URL),
        verbose=bool(_overrides.get("verbose", False)),
        token_override=_overrides.get("token") or os.environ.get("GITLAB_TOKEN"),
    )


def reset() -> None:
    """Drop overrides and the cached instance. For tests."""
    _overrides.clear()
    get_config.cache_clear()
