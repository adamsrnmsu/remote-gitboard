"""gitboard — read a GitLab issue board, or define one in YAML.

The CLI. Everything else in the package is a module it calls:

    config.py   the config singleton (url, tokens, verbosity)
    log.py      the console + logger singletons
    client.py   the GitLab connection and its error messages
    board.py    reading a board (the part no MCP server does)
    apply.py    writing a board from YAML — the only writer

    gitboard show group/project
    gitboard show group/project --markdown | less
    gitboard plan boards/test.yaml
    gitboard apply boards/test.yaml
"""

import os
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.table import Table

from gitboard import apply as apply_mod
from gitboard import board as board_mod
from gitboard import client, history
from gitboard.config import (
    FILENAME,
    ConfigError,
    candidate_paths,
    configure,
    get_config,
)
from gitboard.log import err, get_logger, out, set_verbose

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help=__doc__.split("\n\n")[0],
)
log = get_logger()

SIGN = {"added": "+", "changed": "~"}


@app.callback()
def main(
    url: str | None = typer.Option(
        None, "--url", envvar="GITLAB_URL", help="GitLab instance URL."
    ),
    token: str | None = typer.Option(
        None,
        "--read-token",
        "--token",
        envvar="GITLAB_READ_TOKEN",
        help="Read PAT (read_api scope).",
    ),
    write_token: str | None = typer.Option(
        None,
        "--write-token",
        envvar="GITLAB_WRITE_TOKEN",
        help="PAT for `apply` (api scope). Falls back to --token.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging."),
    config_path: str | None = typer.Option(
        None,
        "--config",
        "-c",
        envvar="GITBOARD_CONFIG",
        help="TOML config file. Default: ./gitboard.toml, then ~/.config/gitboard/.",
    ),
):
    """Configure the singletons once, before any command runs."""
    set_verbose(verbose)
    try:
        cfg = configure(
            url=url,
            token=token,
            write_token=write_token,
            verbose=verbose,
            config_path=config_path,
        )
    except ConfigError as e:
        err().print(f"[logging.level.error]error[/] {e}")
        raise typer.Exit(1) from e
    for warning in cfg.warnings:
        log.warning(warning)
    log.debug("url=%s config=%s", cfg.url, cfg.source or "<none>")


EXAMPLE = {
    "spec": ('spec = "boards/team.yaml"', "gitboard apply boards/team.yaml"),
    "project": ('project = "group/project"', "gitboard show group/project"),
}


def _need(value, key, what):
    """Fall back to the config file, or say exactly how to supply it.

    The old message listed every path it searched and left the reader to work
    out what to do with that. Lead with the two fixes instead; the search path
    is the least useful part and goes last.
    """
    if value:
        return value
    cfg = get_config()
    if fallback := getattr(cfg, key, None):
        return fallback

    line, inline = EXAMPLE[key]
    found = f"config in use: {cfg.source}" if cfg.source else "no config file found"
    where = cfg.source or (candidate_paths()[0])
    raise ConfigError(
        f"no {what} given.\n"
        f"  Pass it directly:   {inline}\n"
        f"  Or set a default:   echo '{line}' >> {where}\n"
        f"  ({found}; searched ./{FILENAME} upwards, then "
        f"~/.config/gitboard/config.toml)"
    )


def _run(fn):
    """Turn the two expected exceptions into one clean line and exit 1."""
    try:
        return fn()
    except (client.GitlabProblem, apply_mod.SpecError, ConfigError) as e:
        err().print(f"[logging.level.error]error[/] {e}")
        raise typer.Exit(1) from e


def _shortest(path):
    """Relative to the cwd when that is shorter — absolute paths are noise."""
    rel = os.path.relpath(str(path), Path.cwd())
    return rel if not rel.startswith("..") else str(path)


def find_spec(project_path):
    """The boards/*.yaml that defines this project, if one is sitting around.

    Config first; otherwise scan boards/ next to the config or the cwd. Purely
    for the "go look here" footer, so any failure just means no footer.
    """
    cfg = get_config()
    for candidate in filter(None, [cfg.spec]):
        try:
            if apply_mod.load(candidate)["project"] == project_path:
                return _shortest(candidate)
        except (apply_mod.SpecError, KeyError):
            pass

    roots = [p.parent for p in [cfg.source] if p] + [Path.cwd()]
    for root in roots:
        for path in sorted((root / "boards").glob("*.yaml")):
            try:
                if apply_mod.load(str(path))["project"] == project_path:
                    return _shortest(path)
            except (apply_mod.SpecError, KeyError):
                continue
    return None


@app.command()
def show(
    project: str | None = typer.Argument(None, help="group/project"),
    board_name: str | None = typer.Argument(None, help="Board name, if several."),
    markdown: bool = typer.Option(
        False, "--markdown", "-m", help="Stable markdown, for piping or the AI pass."
    ),
    all_issues: bool = typer.Option(
        False, "--all", "-a", help="Do not truncate long columns."
    ),
    limit: int = typer.Option(
        5, "--limit", "-n", help="Issues shown per column. 0 for no limit."
    ),
):
    """Print an issue board, grouped into its columns."""

    def go():
        path = _need(project, "project", "project")
        project_obj, board_obj = board_mod.fetch(path, board_name or get_config().board)
        if markdown:
            print(board_mod.as_markdown(project_obj, board_obj))
        else:
            board_mod.print_rich(
                project_obj,
                board_obj,
                spec_path=find_spec(path),
                limit=0 if all_issues else limit,
            )

    _run(go)


@app.command()
def plan(spec: str | None = typer.Argument(None, help="Path to a board YAML file.")):
    """Show what apply would change. Never writes."""

    def go():
        parsed = apply_mod.load(_need(spec, "spec", "spec file"))
        with err().status(f"reading {parsed['project']}…"):
            pending = apply_mod.plan(client.gitlab(), parsed)
        _print_changes(pending, f"pending against {get_config().url}")

    _run(go)


@app.command()
def apply(
    spec: str | None = typer.Argument(None, help="Path to a board YAML file."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Make the board match the YAML. Writes — needs an api-scope token."""

    def go():
        parsed = apply_mod.load(_need(spec, "spec", "spec file"))
        gl = client.gitlab(write=True)
        with err().status(f"reading {parsed['project']}…"):
            pending = apply_mod.plan(gl, parsed)

        if not pending:
            err().print("[muted]no changes — board already matches[/]")
            return
        _print_changes(pending, f"will write to {get_config().url}")
        if not yes and not typer.confirm(f"apply {len(pending)} change(s)?"):
            raise typer.Abort()

        with client.write_errors():
            changes = apply_mod.apply(gl, parsed)
        err().print(
            f"[added]{len(changes)} change(s) written[/] — "
            f"gitboard show {parsed['project']}"
        )

    _run(go)


@app.command("migrate-comments")
def migrate_comments(
    src: int = typer.Argument(..., help="Issue iid to copy comments from."),
    dst: int = typer.Argument(..., help="Issue iid to copy them onto."),
    project: str | None = typer.Option(
        None, "--project", "-p", help="group/project. Defaults to the config."
    ),
):
    """Copy an issue's comments to its successor. Writes — needs api scope."""

    def go():
        path = _need(project, "project", "project")
        gl = client.gitlab(write=True)
        with client.write_errors():
            copied = apply_mod.migrate_comments(gl, path, src, dst)
        if copied:
            err().print(f"[added]{copied} comment(s) copied[/] #{src} -> #{dst}")
        else:
            err().print("[muted]nothing to copy — no comments, or already migrated[/]")

    _run(go)


@app.command()
def snapshot(
    project: str | None = typer.Argument(None, help="group/project"),
    board_name: str | None = typer.Argument(None, help="Board name, if several."),
    out_path: str = typer.Option(
        "snapshots.db", "--out", "-o", help="SQLite file to record into."
    ),
):
    """Record the board's state in a SQLite log. Only changes are written."""

    def go():
        path = _need(project, "project", "project")
        proj, board = board_mod.fetch(path, board_name or get_config().board)
        ts = datetime.now(UTC).isoformat(timespec="seconds")
        records = board_mod.snapshot_records(proj, board, ts)
        written = history.record(out_path, records)
        if written:
            err().print(f"[added]{written} change(s)[/] recorded in {out_path}")
        else:
            err().print(f"[muted]no changes since the last snapshot[/] ({out_path})")

    _run(go)


def _print_changes(pending, title):
    if not pending:
        err().print("[muted]no changes — board already matches[/]")
        return
    table = Table(title=title, title_justify="left", title_style="muted", box=None)
    table.add_column("", width=1)
    table.add_column("", style="muted", width=7)
    table.add_column("")
    for kind, what, detail in pending:
        table.add_row(f"[{kind}]{SIGN[kind]}[/]", what, detail)
    err().print(table)


@app.command()
def config():
    """Show the resolved configuration and where the token would come from."""
    cfg = get_config()
    table = Table(box=None)
    table.add_column("", style="muted")
    table.add_column("")
    table.add_row("config file", str(cfg.source) if cfg.source else "[muted]none[/]")
    table.add_row(".env", str(cfg.env_source) if cfg.env_source else "[muted]none[/]")
    table.add_row("url", cfg.url)
    table.add_row("verbose", str(cfg.verbose))
    for key in ("project", "board", "spec"):
        table.add_row(key, getattr(cfg, key) or "[muted]unset[/]")
    try:
        cfg.token()
        table.add_row("read token", f"[added]found[/] [muted]({cfg.token_source})[/]")
    except ConfigError:
        table.add_row("read token", "[logging.level.error]not found[/]")
    if cfg.write_token_source:
        table.add_row(
            "write token", f"[added]found[/] [muted]({cfg.write_token_source})[/]"
        )
    else:
        table.add_row("write token", "[muted]unset — apply reuses the read token[/]")
    out().print(table)


if __name__ == "__main__":
    app()
