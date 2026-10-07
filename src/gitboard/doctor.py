"""`gitboard doctor`: one line per thing that can be wrong, with the fix.

Read only: nothing here writes to GitLab or to disk. Network and project lookups
are injected (`http`, `resolve`) so tests never touch a real GitLab. No message
carries a token value.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gitboard import apply as apply_mod
from gitboard.config import Config, ConfigError

OK, WARN, FAIL = "ok", "warn", "fail"
BASE_MAX_AGE = timedelta(days=7)
SNAPSHOT_MAX_AGE = timedelta(days=1)

# (api path, token) -> (status, parsed JSON or None); OSError when unreachable
Http = Callable[[str, str | None], tuple[int, object]]
# (project, board) -> None; raises on a project or board that does not resolve
Resolve = Callable[[str, str | None], None]


@dataclass(frozen=True)
class Check:
    status: str  # OK, WARN or FAIL; only WARN and FAIL show `fix`
    what: str
    fix: str = ""


def _scopes(http, token):
    """(status, scopes or None): None = this GitLab will not say (404 etc.)."""
    status, body = http("/api/v4/personal_access_tokens/self", token)
    if status == 200 and isinstance(body, dict):
        return 200, list(body.get("scopes") or [])
    return status, None


def _token_checks(cfg: Config, http: Http) -> list[Check]:
    try:
        token = cfg.token()
    except ConfigError:
        return [
            Check(
                FAIL,
                "read token: not found",
                "set GITLAB_READ_TOKEN in .env, or add a keychain item "
                "'gitlab-read-token'",
            )
        ]
    src = f"read token ({cfg.token_source})"
    status, scopes = _scopes(http, token)
    if status == 401:
        return [
            Check(
                FAIL,
                f"{src}: rejected by {cfg.url}",
                "mint a new token on this instance (read_api scope)",
            )
        ]
    if scopes is None:
        checks = [
            Check(
                WARN,
                f"{src}: scope unknown (no token-introspection endpoint here)",
                "older GitLab or not a personal access token; check it by hand",
            )
        ]
    elif {"read_api", "api"} & set(scopes):
        checks = [Check(OK, f"{src}: valid, scopes {', '.join(scopes)}")]
    else:
        checks = [
            Check(
                FAIL,
                f"{src}: scopes {', '.join(scopes) or 'none'}, needs read_api",
                "mint a token with read_api",
            )
        ]

    # the token `push` would use
    if cfg.write_token_source:
        wsrc = f"write token ({cfg.write_token_source})"
        wstatus, wscopes = _scopes(http, cfg.token(write=True))
    else:
        wsrc = "write token (falls back to the read token)"
        wstatus, wscopes = status, scopes
    if wstatus == 401:
        checks.append(
            Check(FAIL, f"{wsrc}: rejected by {cfg.url}", "mint an api-scope token")
        )
    elif wscopes is None:
        checks.append(Check(WARN, f"{wsrc}: scope unknown", "push needs `api` scope"))
    elif "api" in wscopes:
        checks.append(Check(OK, f"{wsrc}: has api scope"))
    else:
        checks.append(
            Check(
                WARN,
                f"{wsrc}: no api scope, push will be refused",
                "put an api-scope token in GITLAB_WRITE_TOKEN (.env or environment)",
            )
        )
    return checks


def _spec_checks(specs: list[Path], now: datetime) -> list[Check]:
    checks, loaded = [], 0
    for path in specs:
        try:
            apply_mod.load(str(path))
        except apply_mod.SpecError as e:
            checks.append(Check(FAIL, f"{path}: does not load ({e})", f"fix {path}"))
            continue
        loaded += 1
        base = Path(f"{path}.base")
        if not base.exists():
            checks.append(
                Check(
                    WARN,
                    f"{path}: no .base",
                    "gitboard pull --base --force (plan/push are two-way without it)",
                )
            )
            continue
        age = now - datetime.fromtimestamp(base.stat().st_mtime, tz=UTC)
        if age > BASE_MAX_AGE:
            checks.append(
                Check(
                    WARN,
                    f"{base}: {age.days}d old",
                    "gitboard sync, or pull --force, to refresh it",
                )
            )
    if loaded:
        checks.append(Check(OK, f"{loaded} board file(s) load"))
    return checks


def _snapshot_check(db: Path, now: datetime) -> Check:
    fix = "cron not running? make cron"
    stamp = None
    try:
        for line in db.read_text().splitlines():
            stamp = datetime.fromisoformat(json.loads(line)["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        stamp = None
    if stamp is None:
        return Check(WARN, f"{db}: no readable snapshots", fix)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    what = f"{db}: last snapshot {stamp:%Y-%m-%d %H:%M}"
    return Check(WARN if now - stamp > SNAPSHOT_MAX_AGE else OK, what, fix)


def _network_checks(cfg, http, resolve) -> list[Check]:
    try:
        status, body = http("/api/v4/version", None)
        if status == 200 and isinstance(body, dict):
            checks = [Check(OK, f"{cfg.url} reachable, GitLab {body.get('version')}")]
        else:
            checks = [
                Check(
                    WARN,
                    f"{cfg.url}: /api/v4/version answered {status}",
                    "some instances want a token for it; the checks below say more",
                )
            ]
        checks += _token_checks(cfg, http)
        if cfg.project:
            try:
                resolve(cfg.project, cfg.board)
                checks.append(Check(OK, f"project {cfg.project}: board resolves"))
            except Exception as e:  # GitlabProblem text never carries a token
                checks.append(
                    Check(
                        FAIL,
                        f"project {cfg.project}: {e}",
                        "check `project`/`board` in gitboard.toml",
                    )
                )
        mcp, _ = http("/api/v4/mcp", None)
        checks.append(
            Check(
                OK,
                "GitLab MCP endpoint: "
                + ("not present (info only)" if mcp == 404 else "present"),
            )
        )
        return checks
    except OSError as e:
        return [
            Check(
                FAIL,
                f"cannot reach {cfg.url}: {e.__class__.__name__}",
                "check `url` and your network, or pass --offline",
            )
        ]


def run(
    cfg: Config,
    *,
    http: Http,
    resolve: Resolve,
    root: Path,
    specs: list[Path],
    now: datetime | None = None,
    offline: bool = False,
) -> list[Check]:
    now = now or datetime.now(UTC)
    checks = [
        Check(OK, f"config file: {cfg.source}")
        if cfg.source
        else Check(
            WARN, "config file: none found", "create gitboard.toml (url, project)"
        ),
        Check(OK, f"url: {cfg.url}"),
    ]
    if not offline:
        checks += _network_checks(cfg, http, resolve)
    checks += _spec_checks(specs, now)
    checks.append(_snapshot_check(root / "snapshots.jsonl", now))
    return checks
