#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-gitlab", "pyyaml", "typer", "rich", "python-dotenv"]
# ///
"""gitboard — read a GitLab issue board, or define one in YAML.

The single entry point. Everything else in this repo is a module it calls:

    config.py   the config singleton (url, token, verbosity)
    log.py      the console + logger singletons
    client.py   the GitLab connection and its error messages
    board.py    reading a board (the part no MCP server does)
    apply.py    writing a board from YAML — the only writer

    ./gitboard.py show group/project
    ./gitboard.py show group/project --markdown | less
    ./gitboard.py plan boards/test.yaml
    ./gitboard.py apply boards/test.yaml
"""

import typer
from rich.table import Table

import apply as apply_mod
import board as board_mod
import client
from config import ConfigError, candidate_paths, configure, get_config
from log import err, get_logger, out, set_verbose

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
        None, "--token", envvar="GITLAB_TOKEN", help="Read PAT (read_api scope)."
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


def _need(value, key, what):
    """Fall back to the config file, or explain what's missing."""
    if value:
        return value
    if fallback := getattr(get_config(), key, None):
        return fallback
    raise ConfigError(
        f"no {what} given and no '{key}' in a config file "
        f"(tried {', '.join(str(p) for p in candidate_paths())})"
    )


def _run(fn):
    """Turn the two expected exceptions into one clean line and exit 1."""
    try:
        return fn()
    except (client.GitlabProblem, apply_mod.SpecError, ConfigError) as e:
        err().print(f"[logging.level.error]error[/] {e}")
        raise typer.Exit(1) from e


@app.command()
def show(
    project: str | None = typer.Argument(None, help="group/project"),
    board_name: str | None = typer.Argument(None, help="Board name, if several."),
    markdown: bool = typer.Option(
        False, "--markdown", "-m", help="Stable markdown, for piping or the AI pass."
    ),
):
    """Print an issue board, grouped into its columns."""

    def go():
        project_obj, board_obj = board_mod.fetch(
            _need(project, "project", "project"), board_name or get_config().board
        )
        if markdown:
            print(board_mod.as_markdown(project_obj, board_obj))
        else:
            board_mod.print_rich(project_obj, board_obj)

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
            f"./gitboard.py show {parsed['project']}"
        )

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
