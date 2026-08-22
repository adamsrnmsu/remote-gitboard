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

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.table import Table

from gitboard import apply as apply_mod
from gitboard import board as board_mod
from gitboard import client
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
        "snapshots.jsonl", "--out", "-o", help="JSONL file to append to."
    ),
):
    """Append the board's current state to a JSONL log, one line per issue."""

    def go():
        path = _need(project, "project", "project")
        proj, board = board_mod.fetch(path, board_name or get_config().board)
        n = _write_snapshot(proj, board, out_path)
        err().print(f"[added]{n} issue(s)[/] appended to {out_path}")

    _run(go)


def _write_snapshot(proj, board, out_path="snapshots.jsonl"):
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    records = board_mod.snapshot_records(proj, board, ts)
    with open(out_path, "a") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return len(records)


def _pull_spec(proj, board, columns, out_file):
    """Write the live board as YAML. Refuses to clobber an existing file."""
    if Path(out_file).exists():
        raise ConfigError(
            f"{out_file} already exists — edit it, or pass a different --out"
        )
    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    spec = apply_mod.spec_from_board(proj, board, columns)
    Path(out_file).write_text(apply_mod.dump(spec))
    return out_file


@app.command()
def pull(
    project: str | None = typer.Argument(None, help="group/project"),
    board_name: str | None = typer.Argument(None, help="Board name, if several."),
    out: str | None = typer.Option(
        None, "--out", "-o", help="Where to write. Default: boards/<project>.yaml"
    ),
):
    """Save the live board as YAML — the file plan/apply read. Reads only."""

    def go():
        path = _need(project, "project", "project")
        with err().status(f"reading {path}…"):
            proj, board = board_mod.fetch(path, board_name or get_config().board)
            columns = board_mod.board_columns(proj, board)
        target = out or f"boards/{path.rsplit('/', 1)[-1]}.yaml"
        _pull_spec(proj, board, columns, target)
        err().print(
            f"[added]wrote {target}[/] — edit it, then `gitboard plan {target}`"
        )

    _run(go)


def _key():
    """One raw keypress. The whole input layer of the TUI."""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


@app.command()
def tui(
    project: str | None = typer.Argument(None, help="group/project"),
    board_name: str | None = typer.Argument(None, help="Board name, if several."),
):
    """The board, interactively: reload / snapshot / plan / apply / quit."""

    def go():
        if not sys.stdin.isatty():
            raise ConfigError("tui needs a terminal — use `show` in pipes")
        import shlex
        import signal
        import subprocess

        from rich.console import Group
        from rich.live import Live
        from rich.panel import Panel
        from rich.text import Text

        console = err()
        st = {
            "status": None,
            "extra": None,
            "prompt": None,
            "path": _need(project, "project", "project"),
            "name": board_name or get_config().board,
        }

        def refetch():
            st["proj"], st["board"] = board_mod.fetch(st["path"], st["name"])
            st["columns"] = board_mod.board_columns(st["proj"], st["board"])
            st["spec"] = find_spec(st["path"])

        def keybar():
            if st["prompt"]:
                return Text(f"  {st['prompt']}", "bold yellow")
            pairs = [("r", "reload"), ("b", "board"), ("s", "snapshot"), ("e", "edit")]
            if st["spec"]:
                pairs += [("p", "plan"), ("a", "apply")]
            pairs += [("m", "migrate"), ("?", "help"), ("q", "quit")]
            bar = Text("  ")
            for key, label in pairs:
                bar.append(f" {key} ", "bold reverse")
                bar.append(f" {label}", "muted")
                bar.append("   ")
            return bar

        def view():
            cols = st["columns"]
            # ponytail: naive fit — one header + one spare line per column,
            # the rest split evenly. Uneven boards waste a little; fine
            # until someone complains.
            usable = console.size.height - 7 - (2 * len(cols))
            limit = max(2, usable // max(len(cols), 1))
            body, hidden = board_mod.board_view(
                st["proj"], st["board"], limit=limit, columns=cols
            )
            spec = st["spec"]
            parts = [
                Panel(
                    body,
                    subtitle=Text(
                        f"defined by {spec} — e edits, a applies"
                        if spec
                        else "no YAML yet — e pulls the board into one",
                        "muted",
                    ),
                    subtitle_align="left",
                    border_style="cyan",
                    padding=(0, 1),
                )
            ]
            if st["extra"] is not None:
                parts.append(st["extra"])
            if st["status"] is not None:
                parts.append(Text("  ", end="") + st["status"])
            parts.append(keybar())
            return Group(*parts)

        def board_choices():
            choices = [(st["path"], b.name) for b in st["proj"].boards.list(all=True)]
            seen = set(choices)
            cfg = get_config()
            roots = [c.parent for c in [cfg.source] if c] + [Path.cwd()]
            for root in roots:
                for f in sorted((root / "boards").glob("*.yaml")):
                    try:
                        parsed = apply_mod.load(str(f))
                        entry = (parsed["project"], parsed["board"])
                    except (apply_mod.SpecError, KeyError):
                        continue
                    if entry not in seen:
                        seen.add(entry)
                        choices.append(entry)
            return choices

        def read_iid(label):
            """Digits typed into the prompt line, board still on screen."""
            buf = ""
            while True:
                st["prompt"] = f"{label} #{buf}_   (enter confirms, esc cancels)"
                draw()
                k = _key()
                if k in ("\r", "\n"):
                    st["prompt"] = None
                    return int(buf) if buf else None
                if k == "\x1b":
                    st["prompt"] = None
                    return None
                if k in ("\x7f", "\b"):
                    buf = buf[:-1]
                elif k.isdigit():
                    buf += k

        def issue_title(iid):
            for _, issues in st["columns"]:
                for issue in issues:
                    if issue.iid == iid:
                        return issue.title
            return None

        def help_panel():
            lines = [
                ("", "The YAML in boards/ is the source of truth; the board is"),
                ("", "what GitLab currently shows. Editing happens in the YAML."),
                ("r", "refetch the board"),
                ("b", "switch board — this project's, plus any that a"),
                ("", "boards/*.yaml defines (other projects included)"),
                ("s", "append every issue to snapshots.jsonl, the progress log"),
                ("e", "edit the YAML in $EDITOR (pulled from the board if there"),
                ("", "is none yet); the diff is shown when you come back"),
                ("p", "diff the YAML against the board — never writes"),
                ("a", "write the YAML to the board — additive only, y/n first"),
                ("m", "copy a finished issue's comments onto its successor"),
                ("q", "quit"),
            ]
            grid = Table(box=None, show_header=False, padding=(0, 1))
            grid.add_column(style="bold reverse", width=3, justify="center")
            grid.add_column()
            for key, text in lines:
                grid.add_row(f" {key} " if key else "", text)
            return Panel(grid, title="keys", border_style="muted", padding=(0, 1))

        with Live(console=console, screen=True, auto_refresh=False) as live:

            def draw(busy=None):
                if busy:
                    st["status"] = Text(busy, "muted")
                live.update(view() if "proj" in st else Text(""), refresh=True)
                if busy:
                    st["status"] = None

            # a resize re-renders at the new size, including the fit limit
            signal.signal(signal.SIGWINCH, lambda *_: draw())

            live.update(Text(f"reading {st['path']}…", "muted"), refresh=True)
            refetch()
            draw()
            while True:
                k = _key().lower()
                st["status"] = st["extra"] = st["prompt"] = None
                if k == "q":
                    return
                if k == "r":
                    draw(busy=f"reading {st['path']}…")
                    refetch()
                elif k == "s":
                    n = _write_snapshot(st["proj"], st["board"])
                    st["status"] = Text(
                        f"{n} issue(s) appended to snapshots.jsonl", "added"
                    )
                elif k == "?":
                    st["extra"] = help_panel()
                elif k == "b":
                    choices = board_choices()[:9]
                    if len(choices) < 2:
                        st["status"] = Text("nothing else to switch to", "muted")
                    else:
                        # ponytail: single-digit pick caps at 9; past that,
                        # pass the project/board arguments instead
                        current = (st["path"], st["board"].name)
                        grid = Table(box=None, show_header=False, padding=(0, 1))
                        grid.add_column(style="bold reverse", width=3)
                        grid.add_column()
                        for i, (proj_path, name) in enumerate(choices, 1):
                            here = "  ← current" if (proj_path, name) == current else ""
                            grid.add_row(f" {i} ", f"{proj_path} — {name}{here}")
                        st["extra"] = Panel(
                            grid, title="boards", border_style="muted", padding=(0, 1)
                        )
                        st["prompt"] = "which board?  (anything else cancels)"
                        draw()
                        st["prompt"], st["extra"] = None, None
                        pick = _key()
                        if pick.isdigit() and 1 <= int(pick) <= len(choices):
                            st["path"], st["name"] = choices[int(pick) - 1]
                            draw(busy=f"reading {st['path']}…")
                            refetch()
                        else:
                            st["status"] = Text("cancelled", "muted")
                elif k == "e":
                    live.stop()
                    spec = st["spec"]
                    if not spec:
                        spec = f"boards/{st['path'].rsplit('/', 1)[-1]}.yaml"
                        _pull_spec(st["proj"], st["board"], st["columns"], spec)
                        console.print(f"[added]pulled the board into {spec}[/]")
                    editor = shlex.split(
                        os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
                    )
                    subprocess.call([*editor, spec])
                    live.start(refresh=True)
                    st["spec"] = spec
                    parsed = apply_mod.load(spec)
                    draw(busy="comparing…")
                    pending = apply_mod.plan(client.gitlab(), parsed)
                    if pending:
                        st["extra"] = _changes_table(pending, f"{spec} — a applies")
                    else:
                        st["status"] = Text(
                            "no changes — board already matches", "muted"
                        )
                elif k == "m":
                    m_src = read_iid("copy comments from")
                    m_dst = None
                    if m_src is not None:
                        title = issue_title(m_src)
                        source = f"#{m_src}"
                        if title:
                            source += f" “{title[:40]}”"
                        m_dst = read_iid(f"{source}  →  onto")
                    if m_src is None or m_dst is None:
                        st["status"] = Text("cancelled", "muted")
                    else:
                        draw(busy=f"copying #{m_src} -> #{m_dst}…")
                        with client.write_errors():
                            n = apply_mod.migrate_comments(
                                client.gitlab(write=True), st["path"], m_src, m_dst
                            )
                        st["status"] = Text(
                            f"{n} comment(s) copied #{m_src} -> #{m_dst}", "added"
                        )
                elif k in ("p", "a") and st["spec"]:
                    parsed = apply_mod.load(st["spec"])
                    gl = client.gitlab(write=(k == "a"))
                    draw(busy="comparing…")
                    pending = apply_mod.plan(gl, parsed)
                    spec = st["spec"]
                    if not pending:
                        st["status"] = Text(
                            "no changes — board already matches", "muted"
                        )
                    elif k == "p":
                        st["extra"] = _changes_table(pending, f"{spec} — a applies")
                    else:
                        st["extra"] = _changes_table(pending, f"{spec} — will write")
                        st["prompt"] = f"apply {len(pending)} change(s)?  y / n"
                        draw()
                        st["prompt"] = None
                        if _key().lower() == "y":
                            draw(busy="writing…")
                            with client.write_errors():
                                changes = apply_mod.apply(gl, parsed)
                            refetch()
                            st["extra"] = None
                            st["status"] = Text(
                                f"{len(changes)} change(s) written", "added"
                            )
                        else:
                            st["extra"] = None
                            st["status"] = Text("not applied", "muted")
                draw()

    _run(go)


def _changes_table(pending, title):
    table = Table(title=title, title_justify="left", title_style="muted", box=None)
    table.add_column("", width=1)
    table.add_column("", style="muted", width=7)
    table.add_column("")
    for kind, what, detail in pending:
        table.add_row(f"[{kind}]{SIGN[kind]}[/]", what, detail)
    return table


def _print_changes(pending, title):
    if not pending:
        err().print("[muted]no changes — board already matches[/]")
        return
    err().print(_changes_table(pending, title))


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
