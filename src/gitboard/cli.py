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
    gitboard push boards/test.yaml
"""

import codecs
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.table import Table
from rich.text import Text

from gitboard import apply as apply_mod
from gitboard import blocks as blocks_mod
from gitboard import board as board_mod
from gitboard import client, graph_html
from gitboard import edit as edit_mod
from gitboard import estimate as estimate_mod
from gitboard import gantt as gantt_mod
from gitboard import graph as graph_mod
from gitboard import guide as guide_mod
from gitboard import ingest as ingest_mod
from gitboard import mail as mail_mod
from gitboard import migrate as migrate_mod
from gitboard import report as report_mod
from gitboard import stats as stats_mod
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

SIGN = {"added": "+", "changed": "~", "skipped": "-", "drift": "!", "oneway": "!"}
STYLE = {
    "added": "added",
    "changed": "changed",
    "skipped": "muted",
    "drift": "bold red",
    "oneway": "bold red",  # same glyph as drift, same meaning: look first
}
SNAPSHOTS = "snapshots.jsonl"
STATS_LOG = "reports/stats.jsonl"  # one summary row per board per stats/digest run
AGE_WINDOW = 365  # days of snapshot history a time-in-column reading may span
TREND_WEEKS = 8


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
        help="PAT for `push` (api scope). Falls back to --token.",
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
    "spec": ('spec = "boards/team.yaml"', "gitboard push boards/team.yaml"),
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


SUITE = "PI_SUITE"  # set by `perch tui`: app -> {"cwd", "argv"}
SWITCH = {"P": "perch", "B": "budgie"}  # shifted, read before lowercasing
NO_SUITE = "start from perch tui to switch apps"


def _suite_entry(name):
    """$PI_SUITE's entry for ``name``; None when unset, malformed or absent."""
    try:
        entry = json.loads(os.environ.get(SUITE, ""))[name]
        cwd, argv = str(entry["cwd"]), [str(a) for a in entry["argv"]]
    except (ValueError, KeyError, TypeError):
        return None
    return {**entry, "cwd": cwd, "argv": argv} if argv else None


def _switch(entry):
    """Become the other app. Call only once Live and termios are restored."""
    try:
        os.chdir(entry["cwd"])
        os.execvp(entry["argv"][0], entry["argv"])
    except (OSError, ValueError) as e:  # ValueError: an empty program name
        err().print(f"[logging.level.error]error[/] switch failed: {e}")
        raise typer.Exit(1) from e


PI = "pi"  # perch suite's tmux: the server (-L pi) and its session


def _in_suite():
    """Inside `perch suite`: hop to the app's tmux window instead of exec."""
    return os.path.basename(os.environ.get("TMUX", "").split(",")[0]) == PI


def _hop(name, entry):
    """Bring ``name``'s window to the front; tmux's complaint, or None.

    Same entry running there: just select it. Another (another project) or
    none recorded: restart that window only. No window (the app was quit):
    open it. A copy of perch/core/suite.py's rule: a change goes in all three.
    """
    tmux, window = ["tmux", "-L", PI], f"{PI}:={name}"
    key = json.dumps(entry, sort_keys=True)
    env = f"{SUITE}={os.environ.get(SUITE, '')}"
    start = ["-c", entry["cwd"], "-e", env, *entry["argv"]]
    mark = [";", "set-option", "-w", "-t", window, "@entry", key]
    list_windows = [*tmux, "list-windows", "-t", PI, "-F", "#{window_name}\t#{@entry}"]

    def entries():
        listed = subprocess.run(list_windows, capture_output=True, text=True)
        found = {}
        for line in listed.stdout.splitlines():  # the first window of a name wins
            n, tab, e = line.partition("\t")
            if tab:
                found.setdefault(n, e)
        return listed, found

    try:
        listed, found = entries()
        if listed.returncode:  # an empty listing would spawn a duplicate
            return listed.stderr.strip() or "tmux failed"
        if name not in found:
            argv = [*tmux, "new-window", "-t", f"{PI}:", "-n", name, *start]
            argv += mark
        elif found[name] == key:
            argv = [*tmux, "select-window", "-t", window]
        else:
            argv = [*tmux, "respawn-window", "-k", "-t", window, *start, *mark,
                    ";", "select-window", "-t", window]  # fmt: skip
        done = subprocess.run(argv, capture_output=True, text=True)
        if done.returncode:
            return done.stderr.strip() or "tmux failed"
        if found.get(name) != key:
            time.sleep(0.5)  # new-window exits 0 even if the app crashes
            if name not in entries()[1]:
                return f"{name} exited at startup"
    except OSError as e:
        return str(e)
    return None


def _shortest(path):
    """Relative to the cwd when that is shorter — absolute paths are noise."""
    rel = os.path.relpath(str(path), Path.cwd())
    return rel if not rel.startswith("..") else str(path)


def local_specs():
    """[(path, spec)] for every board YAML in reach, one per (project, board).

    The config's own spec first, then boards/ next to the config file, then
    boards/ under the cwd. Unreadable files are skipped: this feeds footers,
    pickers and --all, none of which should die because one YAML is broken.
    """
    cfg = get_config()
    paths = [Path(cfg.spec)] if cfg.spec else []
    for root in [p.parent for p in [cfg.source] if p] + [Path.cwd()]:
        paths += sorted((root / "boards").glob("*.yaml"))
    found, seen = [], set()
    for path in paths:
        try:
            spec = apply_mod.load(str(path))
        except apply_mod.SpecError:
            continue
        key = (spec["project"], spec["board"])
        if key not in seen:
            seen.add(key)
            found.append((_shortest(path), spec))
    return found


def find_spec(project_path):
    """The boards/*.yaml that defines this project, for the "go look here"
    footer — None means no footer."""
    for path, spec in local_specs():
        if spec["project"] == project_path:
            return path
    return None


def _base_of(spec_path):
    """The pull's untouched `<spec>.base`, loaded, or None when there is none."""
    path = Path(f"{spec_path}.base")
    return apply_mod.load(str(path)) if path.exists() else None


def _rotate_base(spec_path):
    """<spec>.base -> <spec>.base.old, so the previous pull stays diffable."""
    base = Path(f"{spec_path}.base")
    if base.exists():
        base.replace(f"{spec_path}.base.old")


def _ages(project, db=SNAPSHOTS):
    """{iid: (column, days)} from the snapshot log, or None without one."""
    if not Path(db).exists():
        return None
    batches = report_mod.load(db, project=project, days=AGE_WINDOW)
    return report_mod.age_days(report_mod.column_ages(batches))


def _ago(then, now):
    secs = (now - then).total_seconds()
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 48 * 3600:
        return f"{int(secs // 3600)}h ago"
    return f"{int(secs // 86400)}d ago"


def _for_each(fn, items):
    """--all: run fn over every item, report failures, stop for none of them."""
    failed = 0
    for item in items:
        try:
            fn(item)
        except (client.GitlabProblem, apply_mod.SpecError, ConfigError) as e:
            err().print(f"[logging.level.error]error[/] {e}")
            failed += 1
    if failed:
        raise typer.Exit(1)


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
    from_file: str | None = typer.Option(
        None, "--from", help="Render a board YAML instead of GitLab. No network."
    ),
):
    """Print an issue board, grouped into its columns."""

    def go():
        columns = None
        if from_file:
            # offline: the pulled YAML is the board; never touches the token
            spec = apply_mod.load(from_file)
            project_obj, board_obj = board_mod.spec_stand_ins(spec)
            columns = board_mod.columns_from_spec(spec, get_config().url)
            spec_path = from_file
        else:
            path = _need(project, "project", "project")
            project_obj, board_obj = board_mod.fetch(
                path, board_name or get_config().board
            )
            spec_path = find_spec(path)
        ages = _ages(project_obj.path_with_namespace)
        if markdown:
            print(
                board_mod.as_markdown(
                    project_obj, board_obj, columns=columns, ages=ages
                )
            )
        else:
            board_mod.print_rich(
                project_obj,
                board_obj,
                spec_path=spec_path,
                limit=0 if all_issues else limit,
                columns=columns,
                ages=ages,
            )

    _run(go)


@app.command()
def plan(
    spec: str | None = typer.Argument(None, help="Path to a board YAML file."),
    against: str | None = typer.Option(
        None,
        "--against",
        help="Diff against another board YAML (a pull --base copy) instead of "
        "GitLab. No network.",
    ),
    base: str | None = typer.Option(
        None,
        "--base",
        help="The pull's untouched copy. Fields GitLab changed since it show as "
        "drift (!), fields it already has as skipped (-). Default: <spec>.base "
        "when it exists.",
    ),
):
    """Show what push would change. Never writes."""

    def go():
        spec_path = _need(spec, "spec", "spec file")
        parsed = apply_mod.load(spec_path)
        if against:
            have = apply_mod.have_from_spec(apply_mod.load(against))
            _print_changes(apply_mod.diff(parsed, have), f"pending against {against}")
            return
        base_spec = apply_mod.load(base) if base else _base_of(spec_path)
        with err().status(f"reading {parsed['project']}…"):
            pending = apply_mod.plan(client.gitlab(), parsed, base=base_spec)
        title = f"pending against {get_config().url}"
        if base_spec:
            title += f" — drift is what moved since {base or spec_path + '.base'}"
        _print_changes(pending, title)

    _run(go)


IGNORE_DRIFT = typer.Option(
    False,
    "--ignore-drift",
    help="Overwrite fields that changed on GitLab since the pull (<spec>.base).",
)


def _confirm_writes(pending, title, yes, ignore_drift):
    """Table, drift guard, y/n. The rows that will be written; [] for none."""
    drift = [c for c in pending if c[0] == "drift"]
    writes = [c for c in pending if c[0] in ("added", "changed")]
    _print_changes(pending, title)
    if drift and not ignore_drift:
        err().print(
            f"[logging.level.error]{len(drift)} field(s) changed on GitLab since "
            "the pull[/] — review, re-run with --ignore-drift to overwrite, or "
            "re-pull"
        )
        raise typer.Exit(1)
    writes += drift if ignore_drift else []
    if not writes:
        if pending:
            err().print("[muted]nothing to write[/]")
        return []
    if not yes and not typer.confirm(f"push {len(writes)} change(s)?"):
        raise typer.Abort()
    return writes


def _staged(parsed, spec_path, offline=None, write=False):
    """The TUI's plan: what push would do, three-way against `<spec>.base`
    like the CLI's plan/push. Offline it is the diff against the .base
    alone (None when there is none)."""
    base = _base_of(spec_path)
    if not offline:
        return apply_mod.plan(client.gitlab(write=write), parsed, base=base)
    if base is None:
        return None
    return apply_mod.diff(parsed, apply_mod.have_from_spec(base))


def _drift_refusal(pending):
    """The TUI has no --ignore-drift: a drift row means refuse, like the CLI."""
    n = sum(1 for c in pending if c[0] == "drift")
    if n:
        return (
            f"{n} field(s) changed on GitLab since the pull — re-pull, or "
            "`gitboard push --ignore-drift`"
        )


def _write_spec(parsed, base, yes, ignore_drift):
    """plan -> table -> y/n -> apply -> snapshot: the core of push and sync.

    Returns (changes, project, board); the last two are None when nothing was
    written, so the caller knows whether the board was fetched.
    """
    gl = client.gitlab(write=True)
    with err().status(f"reading {parsed['project']}…"):
        pending = apply_mod.plan(gl, parsed, base=base)
    title = f"will write to {get_config().url}"
    if not _confirm_writes(pending, title, yes, ignore_drift):
        return [], None, None
    with client.write_errors():
        changes = apply_mod.apply(gl, parsed, base=base, force=ignore_drift)
    err().print(
        f"[added]{len(changes)} change(s) written[/] — "
        f"gitboard show {parsed['project']}"
    )
    proj, board = board_mod.fetch(parsed["project"], parsed["board"])
    n = _write_snapshot(proj, board)
    err().print(f"[muted]{n} issue(s) appended to {SNAPSHOTS}[/]")
    return changes, proj, board


def _refresh_spec(spec_path, parsed, base, proj=None, board=None):
    """The second half of sync: the YAML and its .base move to the live board.
    `proj`/`board` are the post-write fetch, or None when nothing was written."""
    if proj is None:
        proj, board = board_mod.fetch(parsed["project"], parsed["board"])
    columns = board_mod.board_columns(proj, board)
    notes = any("discussion" in i for i in (base or parsed)["issues"])
    _rotate_base(spec_path)
    _pull_spec(proj, board, columns, spec_path, notes=notes, force=True)
    _pull_spec(proj, board, columns, f"{spec_path}.base", notes=notes)


@app.command()
def push(
    spec: str | None = typer.Argument(None, help="Path to a board YAML file."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
    ignore_drift: bool = IGNORE_DRIFT,
):
    """Make the board match the YAML. Writes — needs an api-scope token.

    With a <spec>.base from `pull --base`, a field the team changed on GitLab
    since the pull is drift: shown, and refused unless --ignore-drift.
    """

    def go():
        spec_path = _need(spec, "spec", "spec file")
        parsed = apply_mod.load(spec_path)
        _write_spec(parsed, _base_of(spec_path), yes, ignore_drift)

    _run(go)


@app.command()
def sync(
    spec: str | None = typer.Argument(None, help="Path to a board YAML file."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
    ignore_drift: bool = IGNORE_DRIFT,
):
    """push, snapshot, then refresh the YAML and <spec>.base from GitLab.

    The host's end-of-round step: what the container staged is written, the
    log gets a line, and both files move forward to the live board so the
    next `status`, `plan` and `pull` measure from now. Posted `notes:` come
    back as `discussion:`; a field GitLab kept (skipped) comes back as
    GitLab has it. Nothing is staged after a sync, by construction.
    """

    def go():
        spec_path = _need(spec, "spec", "spec file")
        parsed = apply_mod.load(spec_path)
        base = _base_of(spec_path)
        _, proj, board = _write_spec(parsed, base, yes, ignore_drift)
        _refresh_spec(spec_path, parsed, base, proj, board)
        err().print(
            f"[added]refreshed {spec_path} and its .base[/] from the live board"
        )
        if (Path(spec_path).parent / "issues.jsonl").exists():
            err().print(
                f"[muted]next: bd import {Path(spec_path).parent / 'issues.jsonl'}[/]"
            )

    _run(go)


@app.command()
def migrate(
    file: str = typer.Argument(..., help="A boards/<name>.migration.yaml."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
):
    """Reformat a board: rename/merge/drop labels, reorder or drop columns,
    move cards, split boards. One-way ops are marked !. The /migrate-board
    agent drafts the file; a person runs this."""

    def go():
        mig = migrate_mod.load(file)
        gl = client.gitlab(write=True)
        with err().status(f"reading {mig['project']}…"):
            pending = migrate_mod.plan(gl, mig)
        ops = [c for c in pending if c[0] != "skipped"]
        if not ops:
            err().print("[muted]nothing to migrate — every op is already applied[/]")
            return
        err().print(_changes_table(pending, f"migration {file} — ! rows are one-way"))
        touched = migrate_mod.touched(pending)
        if not yes and not typer.confirm(
            f"run {len(ops)} op(s), {touched} card(s) touched by one-way ops?"
        ):
            raise typer.Abort()

        def record(kind, what, detail):
            err().print(_changes_table([(kind, what, detail)], None))

        with client.write_errors():
            changes = migrate_mod.apply(gl, mig, record)
        proj, board = board_mod.fetch(mig["project"], mig["board"])
        n = _write_snapshot(proj, board)
        err().print(
            f"[added]{len(changes)} op(s) run[/] — gitboard show {mig['project']}; "
            f"[muted]{n} issue(s) appended to {SNAPSHOTS}[/]"
        )

    _run(go)


def _parse_target(arg, default_path):
    """`34` -> (default_path, 34); `grp/proj#34` -> ("grp/proj", 34)."""
    proj, _, iid = arg.rpartition("#")
    try:
        return (proj or default_path), int(iid)
    except ValueError:
        raise ConfigError(
            f"bad destination {arg!r} — use an iid like 34, or group/project#34"
        ) from None


@app.command("migrate-comments")
def migrate_comments(
    src: int = typer.Argument(..., help="Issue iid to copy comments from."),
    dst: list[str] = typer.Argument(
        ..., help="Destinations: an iid, or group/project#iid. One or more."
    ),
    project: str | None = typer.Option(
        None, "--project", "-p", help="group/project. Defaults to the config."
    ),
    close_source: bool = typer.Option(
        False,
        "--close-source",
        help="Close the source issue afterwards, noting its successors.",
    ),
):
    """Copy an issue's comments to its successor(s). Writes — needs api scope."""

    def go():
        path = _need(project, "project", "project")
        gl = client.gitlab(write=True)
        targets = [_parse_target(a, path) for a in dst]
        wheres = []
        with client.write_errors():
            for dst_path, dst_iid in targets:
                copied = apply_mod.migrate_comments(
                    gl, path, src, dst_iid, dst_path=dst_path
                )
                where = f"#{dst_iid}" if dst_path == path else f"{dst_path}#{dst_iid}"
                wheres.append(where)
                if copied:
                    err().print(
                        f"[added]{copied} comment(s) copied[/] #{src} -> {where}"
                    )
                else:
                    err().print(
                        f"[muted]nothing to copy to {where} — "
                        "no comments, or already migrated[/]"
                    )
            if close_source:
                if apply_mod.close_issue(gl, path, src, superseded_by=wheres):
                    err().print(f"[added]closed #{src}[/]")
                else:
                    err().print(f"[muted]#{src} was already closed[/]")

    _run(go)


@app.command()
def snapshot(
    project: str | None = typer.Argument(None, help="group/project"),
    board_name: str | None = typer.Argument(None, help="Board name, if several."),
    out_path: str = typer.Option(
        SNAPSHOTS, "--out", "-o", help="JSONL file to append to."
    ),
    all_boards: bool = typer.Option(
        False, "--all", help="Every board a local boards/*.yaml defines."
    ),
):
    """Append the board's current state to a JSONL log, one line per issue."""

    def one(path, name):
        proj, board = board_mod.fetch(path, name)
        n = _write_snapshot(proj, board, out_path)
        err().print(f"[added]{n} issue(s)[/] of {path} appended to {out_path}")

    def go():
        if all_boards:
            _for_each(lambda ps: one(ps[1]["project"], ps[1]["board"]), local_specs())
            return
        one(_need(project, "project", "project"), board_name or get_config().board)

    _run(go)


def _write_snapshot(proj, board, out_path=SNAPSHOTS):
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    records = board_mod.snapshot_records(proj, board, ts)
    with open(out_path, "a") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
    return len(records)


def _pull_spec(proj, board, columns, out_file, notes=False, force=False):
    """Write the live board as YAML. Refuses to clobber unless told to."""
    if Path(out_file).exists() and not force:
        raise ConfigError(
            f"{out_file} already exists — edit it, pass a different --out, "
            "or --force to overwrite"
        )
    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    spec = apply_mod.spec_from_board(proj, board, columns, notes=notes)
    Path(out_file).write_text(apply_mod.dump(spec))
    return out_file


def _staged_edits(target, existing):
    """What `target` holds that its .base does not (plan's rows); [] with no .base."""
    base_file = Path(f"{target}.base")
    if not base_file.exists():
        return []
    have = apply_mod.have_from_spec(apply_mod.load(str(base_file)))
    return apply_mod.diff(existing, have)


def _pull_board(path, name, target, force, discard_edits, base, notes, no_snapshot):
    """pull's body for one board: the guards, then the write. Prints nothing,
    so the TUI can call it under its live display."""
    if Path(target).exists():
        try:
            existing = apply_mod.load(target)
        except apply_mod.SpecError:
            existing = None  # unreadable: nothing to protect, --force decides
        if existing and existing["project"] != path:
            raise ConfigError(
                f"{target} is the board of {existing['project']}, not {path} "
                "— pass a different --out"
            )
        if existing and force and not discard_edits and _staged_edits(target, existing):
            raise ConfigError(
                f"{target} has edits not in {target}.base — push them "
                "first, or --discard-edits"
            )
    proj, board = board_mod.fetch(path, name)
    columns = board_mod.board_columns(proj, board)
    _pull_spec(proj, board, columns, target, notes=notes, force=force)
    if base:
        # .base, not .yaml: find_spec and the TUI glob boards/*.yaml,
        # and the /board command may only edit *.yaml — the copy
        # stays pristine. The previous one becomes .base.old.
        _rotate_base(target)
        _pull_spec(proj, board, columns, f"{target}.base", notes=notes)
    if not no_snapshot:
        _write_snapshot(proj, board)


@app.command()
def pull(
    project: str | None = typer.Argument(None, help="group/project"),
    board_name: str | None = typer.Argument(None, help="Board name, if several."),
    out: str | None = typer.Option(
        None, "--out", "-o", help="Where to write. Default: boards/<project>.yaml"
    ),
    base: bool = typer.Option(
        False,
        "--base",
        help="Also keep an untouched copy as <out>.base, for `plan --against` "
        "somewhere with no network.",
    ),
    notes: bool = typer.Option(
        False,
        "--notes",
        help="Include each issue's comments as a read-only `discussion:` list "
        "(one more request per issue).",
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing file (refresh a pull)."
    ),
    discard_edits: bool = typer.Option(
        False,
        "--discard-edits",
        help="With --force: overwrite even when the file has edits its .base "
        "does not (edits that were never pushed).",
    ),
    no_snapshot: bool = typer.Option(
        False, "--no-snapshot", help=f"Do not append the board to {SNAPSHOTS}."
    ),
    all_boards: bool = typer.Option(
        False,
        "--all",
        help="Refresh every local boards/*.yaml in place (implies --force; the "
        "edits guard still holds).",
    ),
):
    """Save the live board as YAML — the file plan/push read. Reads only."""

    def one(path, name, target, force):
        with err().status(f"reading {path}…"):
            _pull_board(
                path, name, target, force, discard_edits, base, notes, no_snapshot
            )
        err().print(
            f"[added]wrote {target}[/] — edit it, then `gitboard plan {target}`"
            + (f" --against {target}.base" if base else "")
        )

    def go():
        if all_boards:
            _for_each(
                lambda ps: one(ps[1]["project"], ps[1]["board"], ps[0], True),
                local_specs(),
            )
            return
        path = _need(project, "project", "project")
        target = out or f"boards/{path.rsplit('/', 1)[-1]}.yaml"
        one(path, board_name or get_config().board, target, force)

    _run(go)


@app.command()
def estimate(
    spec: str | None = typer.Argument(
        None, help="Board YAML. Defaults to the config spec."
    ),
    history_file: str | None = typer.Option(
        None, "--history", help="A `stats --dump` file instead of GitLab. No network."
    ),
):
    """Stage due dates from each person's history into a board YAML. Local only."""

    def go():
        spec_path = _need(spec, "spec", "spec file")
        sp = apply_mod.load(spec_path)
        history, _, _ = _history(
            sp["project"], sp["board"], estimate_mod.HISTORY_DAYS // 2, history_file
        )
        out = estimate_mod.suggest(sp, history, datetime.now(UTC).date())
        table = Table(box=None)
        for head in ("card", "assignee", "due", "basis"):
            table.add_column(head)
        for r in out["rows"]:
            card = f"#{r['iid']} {r['title']}" if r["iid"] else f"(new) {r['title']}"
            table.add_row(card, r["assignee"], r["due"], r["basis"])
        if out["rows"]:
            err().print(table)
        if out["changed"]:
            Path(spec_path).write_text(apply_mod.dump(sp))
            err().print(
                f"[added]{len(out['rows'])} due date(s) staged[/] -> "
                f"{_shortest(spec_path)} — `gitboard plan` shows them"
            )
        elif out["rows"]:
            err().print("[muted]estimates.suggest_due is false: nothing written[/]")
        else:
            err().print(
                "[muted]nothing to estimate: every assigned card has a date, or "
                "too little finished history stands behind it[/]"
            )

    _run(go)


@app.command()
def ingest(
    tasks: str = typer.Argument(
        ..., help="A tasks.md: a heading per person, checkboxes."
    ),
    into: str | None = typer.Option(
        None, "--into", help="Board YAML to fold it into. Defaults to the config spec."
    ),
    source: str | None = typer.Option(
        None, "--source", help="Lineage label in the Source footer. Default: the file."
    ),
    column: str = typer.Option("Verify", "--column", help="Column for open tasks."),
    done: str = typer.Option("Done", "--done", help="Column for `verified` tasks."),
    failed: str = typer.Option("Failed", "--failed", help="Column for `failed` tasks."),
):
    """Fold a tasks.md into a board YAML. Local files only, never GitLab."""

    def go():
        spec_path = _need(into, "spec", "spec file")
        spec = apply_mod.load(spec_path)
        try:
            text = Path(tasks).read_text()
        except OSError as e:
            raise ConfigError(f"cannot read {tasks}: {e.strerror}") from e
        parsed = ingest_mod.parse(text)
        if not parsed:
            raise ConfigError(f"{tasks}: no `- [ ]` tasks found")
        today = datetime.now(UTC).date().isoformat()
        out = ingest_mod.merge(
            spec,
            parsed,
            source or _shortest(tasks),
            today,
            column=column,
            done=done,
            failed=failed,
        )
        short = _shortest(spec_path)
        if out["changed"]:
            # ponytail: safe_dump drops YAML comments; pulled specs have none,
            # hand-written ones lose theirs — fine until someone minds
            Path(spec_path).write_text(apply_mod.dump(spec))
        err().print(
            f"[added]{out['added']} issue(s) added[/], {out['moved']} moved by "
            f"verdict, {out['notes']} note(s) staged, {out['reverify']} to "
            f"re-verify, {out['stale']} stale -> "
            f"{short if out['changed'] else 'nothing changed, file untouched'}"
        )
        if out["unverified"]:
            err().print(
                "[muted]\\[x] without a `verified` comment, not moved: "
                f"{'; '.join(out['unverified'])}[/]"
            )
        for new, old, ratio in out["similar"]:
            err().print(
                f"[muted]not added: '{new}' looks like '{old}' ({ratio:.0%}) — "
                "give it an `id:` or a distinct title[/]"
            )
        for old, new in out["retitled"]:
            err().print(
                f"[muted]same id, title differs; kept '{old}' (file: '{new}')[/]"
            )
        if out["reverify"]:
            err().print(
                "[muted]commit changed: labelled `re-verify` until the next verdict[/]"
            )
        if out["stale"]:
            err().print("[muted]no longer in the file: labelled `stale`, not moved[/]")
        if out["unmapped"]:
            err().print(
                f"[muted]no username for {', '.join(out['unmapped'])} — add them "
                f"under `people:` in {short} to assign[/]"
            )
        err().print(f"[muted]next: gitboard plan {short}[/]")

    _run(go)


@app.command()
def report(
    project: str | None = typer.Argument(None, help="group/project"),
    days: int = typer.Option(7, "--days", "-d", help="Window, in days."),
    db: str = typer.Option("snapshots.jsonl", "--db", help="Snapshot log to read."),
    repo: str | None = typer.Option(
        None, "--repo", help="Git repo to correlate commits against."
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        metavar="SPEC",
        help="Window starts at the pull: SPEC.base's mtime. Replaces --days; "
        "the project defaults to SPEC's.",
    ),
    all_boards: bool = typer.Option(
        False,
        "--all",
        help="Every project a local boards/*.yaml defines, each since its own "
        ".base when it has one.",
    ),
    history: str | None = typer.Option(
        None,
        "--history",
        metavar="FILE",
        help="A `stats --dump` file: adds a verified column crediting verdict "
        "notes to whoever wrote them, over the same window.",
    ),
):
    """What moved on the board, from the snapshot log. Reads only local files.

    The tally credits the issue's assignee; `--history` adds a verified
    column crediting the verifiers, which the snapshot log cannot see.
    """

    def since_ts(spec_path):
        base_file = Path(f"{spec_path}.base")
        if not base_file.exists():
            return None
        mtime = datetime.fromtimestamp(base_file.stat().st_mtime, UTC)
        return mtime.isoformat(timespec="seconds")

    def go():
        if all_boards:
            _for_each(
                lambda ps: _report(
                    ps[1]["project"], days, db, repo, since_ts(ps[0]), history
                ),
                local_specs(),
            )
            return
        ts = None
        if since:
            ts = since_ts(since)
            if ts is None:
                raise ConfigError(f"no {since}.base — `gitboard pull --base` first")
            path = project or apply_mod.load(since)["project"]
        else:
            path = _need(project, "project", "project")
        _report(path, days, db, repo, ts, history)

    _run(go)


def _report(path, days, db, repo, since_ts=None, history=None):
    """One project's report; `since_ts` (ISO) replaces the --days window.

    `history` is a `stats --dump` path; its verdicts are windowed against
    the dump's `fetched_at`, so a replay gives the same numbers.
    """
    verifiers = None
    if history:
        hist, _, meta = _history(path, None, days, from_file=history)
        end = stats_mod.parse_ts(meta["fetched_at"])
        start = stats_mod.parse_ts(since_ts) if since_ts else end - timedelta(days=days)
        verifiers = report_mod.verifier_counts(hist, start, end)
    if since_ts:
        batches = report_mod.since(
            report_mod.load(db, project=path, days=AGE_WINDOW), since_ts
        )
        window = f"since the pull at {since_ts[:16]}"
    else:
        batches = report_mod.load(db, project=path, days=days)
        window = f"within {days} day(s)"
    console = out()
    if not batches:
        err().print(
            f"[muted]no snapshot of {path} {window} in {db} — run "
            "`gitboard snapshot`[/]"
        )
        return
    if len(batches) < 2:
        err().print(
            f"[muted]need two snapshots of {path} {window} in {db} — run "
            "`gitboard snapshot`, wait for movement, run it again[/]"
        )
    else:
        console.print(f"[bold]{path}[/] — {len(batches)} snapshots {window}")
        _movement(console, batches, days, repo, verifiers)
    latest = batches[-1]
    hits = report_mod.stuck(report_mod.column_ages(batches))
    if hits:
        console.print("[bold red]stuck[/] [muted]— past the column's threshold[/]")
        for iid, col, age in hits:
            console.print(
                f"  [muted]#{iid}[/] {latest[iid]['title']}  [muted]{col} for {age}d[/]"
            )


def _movement(console, batches, days, repo, verifiers=None):
    """The moved/new/closed table and the per-assignee tally.

    `verifiers` ({username: verdicts}, from --history) adds a verified
    column; verifiers who are not assignees are listed after, like
    unmatched git authors.
    """
    changes = report_mod.diff(batches)

    table = Table(box=None)
    table.add_column("", style="muted", width=8)
    table.add_column("")
    for before, after in changes["moved"]:
        table.add_row(
            f"#{after['iid']}",
            f"{after['title']}  [muted]{'+'.join(before['columns'])} ->[/] "
            f"{'+'.join(after['columns'])}",
        )
    for rec in changes["new"]:
        table.add_row(
            f"#{rec['iid']}",
            f"{rec['title']}  [added]new[/] [muted]in {'+'.join(rec['columns'])}[/]",
        )
    for rec in changes["closed"]:
        table.add_row(f"#{rec['iid']}", f"{rec['title']}  [muted]closed[/]")
    if table.row_count:
        console.print(table)
    else:
        console.print("[muted]no movement in the window[/]")
    console.print(f"[muted]{len(changes['unchanged'])} issue(s) did not move[/]")

    tally = report_mod.by_assignee(changes)
    if not tally and not verifiers:
        return
    authors = report_mod.commit_counts(repo, days) if repo else {}
    who = Table(box=None)
    who.add_column("assignee", style="bold")
    for col in ("moved", "new", "closed"):
        who.add_column(col, justify="right")
    if repo:
        who.add_column("commits", justify="right")
    if verifiers is not None:
        who.add_column("verified", justify="right")
    for name in sorted(tally, key=lambda n: -sum(tally[n].values())):
        row = [name] + [str(tally[name][c] or "") for c in ("moved", "new", "closed")]
        if repo:
            author = report_mod.match_author(name, authors)
            row.append(str(authors.pop(author)) if author else "?")
        if verifiers is not None:
            row.append(str(verifiers.pop(name, "") or ""))
        who.add_row(*row)
    console.print(who)
    for (a_name, email), count in sorted(authors.items()):
        console.print(
            f"[muted]{count} commit(s) by {a_name} <{email}> matched no assignee[/]"
        )
    for name, count in sorted((verifiers or {}).items()):
        console.print(f"[muted]{count} verdict(s) by {name} matched no assignee[/]")


def _history(project, board_name, days, from_file=None, dump=None, history_days=None):
    """(history, columns, meta): the issue history behind stats and digest.

    `--from` reads a dump and never opens a connection; otherwise fetch back
    twice `days` so the trend has its previous period. `dump` writes the
    same JSON `--from` reads. `meta["fetched_at"]` is the clock every
    number is measured against, so a dump replays identically.
    """
    if from_file:
        meta = json.loads(Path(from_file).read_text())
        return meta["history"], meta["columns"], meta
    now = datetime.now(UTC)
    proj, board = board_mod.fetch(project, board_name)
    # far enough back that a weekly run still has samples to estimate from
    since = now - timedelta(
        days=max(2 * days, history_days or estimate_mod.HISTORY_DAYS)
    )
    history, columns = board_mod.fetch_history(proj, board, since=since)
    meta = {
        "project": proj.path_with_namespace,
        "board": board.name,
        "columns": columns,
        "fetched_at": now.isoformat(),  # keep the fraction: `_in` is half-open
        "since": since.isoformat(),  # start of the fetched window; absent in old dumps
        "milestones": board_mod.active_milestones(proj),
        "history": history,
    }
    if dump:
        Path(dump).write_text(json.dumps(meta, indent=2))
        err().print(f"[muted]wrote {dump}[/]")
    return history, columns, meta


def _summary(history, columns, meta, days, log=STATS_LOG):
    """(summary, now) for the `days` ending at the fetch.

    Every run leaves one row in the stats log — one per board per week, the
    latest run winning — so trends accumulate without a second fetch.
    """
    now = stats_mod.parse_ts(meta["fetched_at"])
    summary = stats_mod.summarise(
        history, columns, now - timedelta(days=days), now, now
    )
    spec_path = find_spec(meta["project"])
    cfg = estimate_mod.config(apply_mod.load(spec_path) if spec_path else {})
    summary["flow"]["tight"] = estimate_mod.tight(history, columns, now, cfg)
    summary["flow"]["late_milestones"] = estimate_mod.late_milestones(
        history, columns, now, cfg
    )
    stats_mod.append_row(
        log,
        stats_mod.stat_row(summary, meta["project"], meta["board"], meta["fetched_at"]),
    )
    return summary, now


def _weekly(project, board=None, log=STATS_LOG):
    """The last TREND_WEEKS rows for a board, or None when nothing is logged."""
    rows = stats_mod.weekly(stats_mod.load_rows(log), project, TREND_WEEKS, board)
    return rows or None


@app.command()
def stats(
    project: str | None = typer.Argument(None, help="group/project"),
    board_name: str | None = typer.Argument(None, help="Board name, if several."),
    days: int = typer.Option(7, "--days", "-d", help="Window, in days."),
    from_file: str | None = typer.Option(
        None, "--from", help="Read a --dump file instead of GitLab. No network."
    ),
    dump: str | None = typer.Option(
        None, "--dump", help="Save the fetched history as JSON for --from."
    ),
    history_days: int | None = typer.Option(
        None,
        "--history-days",
        help="How far back the fetch (and --dump) reaches; the summary keeps --days.",
    ),
    as_json: bool = typer.Option(False, "--json", help="The summary dict as JSON."),
    weeks: int | None = typer.Option(
        None,
        "--weeks",
        help="Only the trend table for the last N weeks, from reports/stats.jsonl. "
        "No network.",
    ),
):
    """Team numbers: open, done, cycle and verify times, flow. Markdown to stdout."""

    def go():
        if weeks:
            path = _need(project, "project", "project")
            rows = stats_mod.weekly(stats_mod.load_rows(STATS_LOG), path, weeks)
            out = f"# {path} — last {weeks} weeks\n\n" + stats_mod.render_weekly_md(
                rows
            )
            if ms := stats_mod.render_milestone_trend_md(rows):
                out += "\n\n## By milestone: open (+added)\n\n" + ms
            print(out)
            return
        path = None if from_file else _need(project, "project", "project")
        history, columns, meta = _history(
            path, board_name or get_config().board, days, from_file, dump, history_days
        )
        summary, _ = _summary(history, columns, meta, days)
        if as_json:
            print(json.dumps(summary, default=str, indent=2))
        else:
            weekly = _weekly(meta["project"], meta["board"])
            if blocks_mod.wanted():
                blocks_mod.emit(stats_mod.team_blocks(summary, weekly))
            else:
                print(stats_mod.render_team_md(summary, weekly=weekly))

    _run(go)


@app.command()
def digest(
    project: str | None = typer.Argument(None, help="group/project"),
    board_name: str | None = typer.Argument(None, help="Board name, if several."),
    days: int = typer.Option(7, "--days", "-d", help="Window, in days."),
    out_dir: str = typer.Option("reports", "--out", help="Directory to write under."),
    all_boards: bool = typer.Option(
        False, "--all", help="Every project a local boards/*.yaml defines."
    ),
    sender: str | None = typer.Option(
        None, "--sender", help="From: for the .eml files."
    ),
    from_file: str | None = typer.Option(
        None, "--from", help="Read a `stats --dump` file instead of GitLab."
    ),
    md_only: bool = typer.Option(
        False, "--md-only", help="Markdown and plain-text .eml only; no HTML."
    ),
):
    """Write the weekly digest: team.md/.html, one .md/.html per person, a
    multipart .eml where the board YAML's `emails:` names an address, and an
    index.html to preview every mail in a browser. Paths on stderr."""

    def one(path, name):
        history, columns, meta = _history(path, name, days, from_file)
        summary, now = _summary(history, columns, meta, days)
        folder = (
            Path(out_dir) / now.date().isoformat() / stats_mod.slug(meta["project"])
        )
        folder.mkdir(parents=True, exist_ok=True)
        spec_path = find_spec(meta["project"])
        emails = (apply_mod.load(spec_path).get("emails") or {}) if spec_path else {}
        people = (
            set(summary["open"]["by_assignee"])
            | set(summary["throughput"]["done_by"]["assignee"])
            | set(summary["verify"]["verifiers"])
        ) - {None, "unassigned"}
        start = stats_mod.parse_ts(summary["period"]["start"])
        series = (
            None if md_only else stats_mod.daily_series(history, columns, start, now)
        )
        week = summary["period"]["start"][:10]
        weekly = _weekly(meta["project"], meta["board"])
        cfg = estimate_mod.config(apply_mod.load(spec_path) if spec_path else {})
        plan = gantt_mod.bars(history, columns, now, cfg)
        ms, today = meta.get("milestones", ()), now.date()

        def write(name, text):
            (folder / name).write_text(text)
            written.append(folder / name)

        written, entries = [], []
        write("team.md", stats_mod.render_team_md(summary, weekly=weekly))
        if series is not None:
            write(
                "team.html",
                mail_mod.render_team_html(
                    summary,
                    series,
                    svg=True,
                    weekly=weekly,
                    gantt=gantt_mod.team_blocks(plan, ms, today, people),
                ),
            )
            entries.append(
                {"name": "team", "files": {"md": "team.md", "html": "team.html"}}
            )
        for who in sorted(people):
            person = stats_mod.for_person(summary, history, who, now)
            body = stats_mod.render_person_md(person, summary, who)
            body += gantt_mod.person_text(plan, ms, today, who)  # also the .eml text
            write(f"{who}.md", body)
            to = emails.get(who)
            subject = f"[{meta['project']}] week of {week} — {who}"
            files = {"md": f"{who}.md"}
            html = None
            if series is not None:
                mine = gantt_mod.person_blocks(plan, ms, today, who)
                heads = {"To": to or "(no email in emails:)", "Subject": subject}
                write(
                    f"{who}.html",
                    mail_mod.render_person_html(
                        person,
                        summary,
                        who,
                        series,
                        svg=True,
                        headers=heads,
                        weekly=weekly,
                        gantt=mine,
                    ),
                )
                html = mail_mod.render_person_html(
                    person, summary, who, series, weekly=weekly, gantt=mine
                )
                files["html"] = f"{who}.html"
            if to:
                write(f"{who}.eml", stats_mod.eml(to, subject, body, sender, now, html))
                files["eml"] = f"{who}.eml"
            entries.append(
                {"name": who, "to": to or "", "subject": subject, "files": files}
            )
        g = graph_mod.build(history, meta.get("milestones", ()))
        if series is not None and _drawable(g):
            title = f"{meta['project']} — blockers and milestones"
            write(
                "graph.html",
                graph_html.render_html(g, title, _flagged(summary["flow"])),
            )
            entries.append({"name": "graph", "files": {"html": "graph.html"}})
        if series is not None and plan:
            every = gantt_mod.team_blocks(plan, ms, today, people, cap=None)
            write(
                "gantt.html", gantt_mod.render_page(f"{meta['project']} — gantt", every)
            )
            entries.append({"name": "gantt", "files": {"html": "gantt.html"}})
        if series is not None:
            write("index.html", mail_mod.render_index_html(entries))
        for w in written:
            err().print(f"[muted]wrote {_shortest(w)}[/]")

    def go():
        if all_boards:
            _for_each(lambda ps: one(ps[1]["project"], ps[1]["board"]), local_specs())
            return
        path = None if from_file else _need(project, "project", "project")
        one(path, board_name or get_config().board)

    _run(go)


def _drawable(g):
    """Worth drawing: a blocker link or a milestone, even one with no cards."""
    return bool(g["edges"]) or any(
        n["kind"] == "milestone" for n in g["nodes"].values()
    )


def _flagged(found):
    """Graph keys of both cards each flag names: what the page draws red.
    `no_milestone` is left out: it would redden every unplanned card."""
    keys = set()
    for item in (x for f in graph_mod.FLAGS if f != "no_milestone" for x in found[f]):
        keys.add(
            str(item["iid"]) if item["iid"] is not None else f"new:{item['title']}"
        )
        if item["blocker"]:
            keys.add(item["blocker"])
    return keys


@app.command()
def graph(
    project: str | None = typer.Argument(None, help="group/project"),
    board_name: str | None = typer.Argument(None, help="Board name, if several."),
    from_file: str | None = typer.Option(
        None, "--from", help="Draw a board YAML instead of GitLab. No network."
    ),
    milestone: str | None = typer.Option(
        None, "--milestone", "-M", help="Only this milestone and what feeds it."
    ),
    html: str | None = typer.Option(
        None, "--html", help="Write a self-contained interactive page here."
    ),
    mermaid: bool = typer.Option(
        False, "--mermaid", help="Print a Mermaid flowchart to stdout."
    ),
):
    """Draw how cards block each other on the way to their milestones."""

    def go():
        if from_file:
            spec = apply_mod.load(from_file)
            cards = graph_mod.cards_from_spec(spec, get_config().url)
            columns = [c["name"] for c in spec["columns"]]
            name = spec["project"]
            known = spec.get("milestones") or []
        else:
            path = _need(project, "project", "project")
            proj, board = board_mod.fetch(path, board_name or get_config().board)
            since = datetime.now(UTC) - timedelta(days=30)
            cards, columns = board_mod.fetch_history(proj, board, since=since)
            name = proj.path_with_namespace
            known = board_mod.active_milestones(proj)
        g = graph_mod.build(cards, known)
        if milestone:
            known = sorted(
                n["title"] for n in g["nodes"].values() if n["kind"] == "milestone"
            )
            if milestone not in known:
                raise ConfigError(
                    f"no milestone {milestone!r}; have: {', '.join(known) or 'none'}"
                )
            g = graph_mod.subgraph(g, milestone)
        if not _drawable(g):
            err().print("[muted]no blockers or milestones on this board[/]")
            return
        flagged = _flagged(graph_mod.flags(cards, columns))
        if html:
            title = f"{name} — {milestone or 'blockers and milestones'}"
            Path(html).write_text(graph_html.render_html(g, title, flagged))
            err().print(f"[muted]wrote {html}[/]")
        elif mermaid:
            print(graph_mod.render_mermaid(g), end="")
        else:
            today = datetime.now().date().isoformat()
            late = None
            if not from_file:  # a spec has no history to forecast from
                spec_path = find_spec(name)
                cfg = estimate_mod.config(
                    apply_mod.load(spec_path) if spec_path else {}
                )
                late = {
                    m["milestone"]: m["days_late"]
                    for m in estimate_mod.late_milestones(
                        cards, columns, datetime.now(UTC), cfg
                    )
                }
            out().print(graph_mod.render_tree(g, today, flagged, late))

    _run(go)


ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}
_pending: list[str] = []
_decoder = codecs.getincrementaldecoder("utf-8")(errors="ignore")


def _split_keys(text):
    """Raw terminal input -> keys: an arrow (`ESC [ A` or `ESC O A`) by name,
    everything else one character each, so a lone ESC still cancels."""
    out, i = [], 0
    while i < len(text):
        seq = text[i : i + 3]
        if len(seq) == 3 and seq[0] == "\x1b" and seq[1] in "[O" and seq[2] in ARROWS:
            out.append(ARROWS[seq[2]])
            i += 3
        else:
            out.append(text[i])
            i += 1
    return out


def _key():
    """One keypress. The whole input layer of the TUI.

    Reads the fd directly: an arrow arrives as three bytes in one read, and
    `sys.stdin.read(1)` would hand them over as ESC plus two stray letters.
    A paste or fast typing arrives as several keys at once; the rest wait
    in `_pending`.
    """
    import termios
    import tty

    fd = sys.stdin.fileno()
    while not _pending:
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            data = os.read(fd, 1024)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        if not data:
            raise EOFError("stdin closed")
        _pending.extend(_split_keys(_decoder.decode(data)))
    return _pending.pop(0)


def _find_card(columns, card, limit, prefer=0):
    """Where `card` is now, as a cursor — so the selection follows a card a
    reload re-sorted or moved. Same number (or title, before it has one);
    the column it was in wins when it is on screen twice; None when it is
    gone or past the shown `limit`."""
    hits = [
        (c, r)
        for c, (_, issues) in enumerate(columns)
        for r, issue in enumerate(issues[: limit or None])
        if (issue.iid, issue.title) == (card.iid, card.title)
        or (card.iid is not None and issue.iid == card.iid)
    ]
    return min(hits, key=lambda h: (h[0] != prefer, h), default=None)


def _move_cursor(cursor, key, sizes):
    """(column, row) after an arrow. `sizes` is the shown card count per
    column; empty columns are skipped, both directions wrap, and a cursor
    that no longer points at a card starts over at the first one."""
    cols = [c for c, n in enumerate(sizes) if n]
    if not cols:
        return None
    if cursor is None or cursor[0] not in cols or cursor[1] >= sizes[cursor[0]]:
        return (cols[0], 0)
    col, row = cursor
    at = cols.index(col)
    if key == "down":
        if row + 1 < sizes[col]:
            return (col, row + 1)
        return (cols[(at + 1) % len(cols)], 0)
    if key == "up":
        if row > 0:
            return (col, row - 1)
        prev = cols[(at - 1) % len(cols)]
        return (prev, sizes[prev] - 1)
    new = cols[(at + (1 if key == "right" else -1)) % len(cols)]
    return (new, min(row, sizes[new] - 1))


@app.command()
def tui(
    project: str | None = typer.Argument(None, help="group/project"),
    board_name: str | None = typer.Argument(None, help="Board name, if several."),
    from_file: str | None = typer.Option(
        None,
        "--from",
        help="Offline: the YAML is the board. Board keys r/e/p, card keys "
        "v/u/d/c/n; the diff is against <file>.base when it exists. Nothing "
        "here touches GitLab.",
    ),
    no_guide: bool = typer.Option(
        False,
        "--no-guide",
        help="Hide the per-mode guide panels (also: guide = false in "
        "gitboard.toml, GITBOARD_GUIDE=0, or g inside).",
    ),
):
    """The board, interactively: card keys stage into the YAML, a pushes it."""

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
        offline = from_file
        base = f"{from_file}.base" if from_file else None
        st = {
            "status": None,
            "extra": None,
            "prompt": None,
            "tip": None,
            "cursor": None,
            "limit": 5,
            "staged": [],
            "guide": get_config().guide and not no_guide,
            "path": from_file or _need(project, "project", "project"),
            "name": board_name or get_config().board,
        }

        def refetch():
            held = selected_issue() if "columns" in st else None
            # all or nothing: a failed read leaves the last board whole
            if offline:
                spec = apply_mod.load(offline)
                proj, board = board_mod.spec_stand_ins(spec)
                columns = board_mod.columns_from_spec(spec, get_config().url)
                found, ages = offline, _ages(spec["project"])
            else:
                proj, board = board_mod.fetch(st["path"], st["name"])
                columns = board_mod.board_columns(proj, board)
                found, ages = find_spec(st["path"]), _ages(proj.path_with_namespace)
            st.update(proj=proj, board=board, columns=columns, spec=found, ages=ages)
            if held is not None:
                st["cursor"] = _find_card(
                    st["columns"], held, st["limit"], prefer=st["cursor"][0]
                )

        def staged(parsed, write=False):
            return _staged(parsed, st["spec"], offline, write)

        def keybar():
            if st["prompt"]:
                return Text(f"  {st['prompt']}", "bold yellow")
            bar = Text()
            heads = ("board", "card ")
            for head, pairs in zip(
                heads, guide_mod.rows(offline, st["spec"]), strict=True
            ):
                bar.append(f"  {head} ", "muted")
                for key, label in pairs:
                    bar.append(f" {key} ", "bold reverse")
                    bar.append(f" {label}", "muted")
                    bar.append("  ")
                bar.append("\n")
            bar.rstrip()
            return bar

        def view():
            cols = st["columns"]
            # ponytail: naive fit — one header + one spare line per column,
            # the rest split evenly. Uneven boards waste a little; fine
            # until someone complains.
            usable = console.size.height - 9 - (2 * len(cols))
            limit = st["limit"] = max(2, usable // max(len(cols), 1))
            cur = st["cursor"]
            body, hidden = board_mod.board_view(
                st["proj"],
                st["board"],
                limit=limit,
                columns=cols,
                ages=st["ages"],
                selected=(cols[cur[0]][0], cur[1]) if selected_issue() else None,
            )
            spec = st["spec"]
            if offline:
                have_base = "p diffs against it" if Path(base).exists() else "no .base"
                subtitle = (
                    f"offline — {spec} is the board; {have_base}; the host pushes"
                )
            elif spec:
                subtitle = f"defined by {spec} — e edits, a pushes"
            else:
                subtitle = "no YAML yet — e pulls the board into one"
            parts = [
                Panel(
                    body,
                    subtitle=Text(subtitle, "muted"),
                    subtitle_align="left",
                    border_style="cyan",
                    padding=(0, 1),
                )
            ]
            if st["tip"] is not None:
                parts.append(st["tip"])
            if st["extra"] is not None:
                parts.append(st["extra"])
            if st["status"] is not None:
                parts.append(Text("  ") + st["status"])
            parts.append(keybar())
            return Group(*parts)

        def board_choices():
            choices = [(st["path"], b.name) for b in st["proj"].boards.list(all=True)]
            seen = set(choices)
            for _, parsed in local_specs():
                entry = (parsed["project"], parsed["board"])
                if entry not in seen:
                    seen.add(entry)
                    choices.append(entry)
            return choices

        def read_buf(label, hint, accept, extra="", mark=""):
            """Text typed into the prompt line, board still on screen.

            The buffer on enter, None on esc. A key from `extra` pressed on
            an empty buffer comes back as `("key", k)`, so callers can offer
            escapes like b-for-board without a mode.
            """
            buf = ""
            while True:
                st["prompt"] = f"{label} {mark}{buf}_   ({hint})"
                draw()
                k = _key()
                if k in ("\r", "\n"):
                    st["prompt"] = None
                    return buf
                if k == "\x1b":
                    st["prompt"] = None
                    return None
                if not buf and extra and k.lower() in extra:
                    st["prompt"] = None
                    return ("key", k.lower())
                if k in ("\x7f", "\b"):
                    buf = buf[:-1]
                elif len(k) == 1 and accept(k):  # an arrow is a name, not text
                    buf += k

        def read_iid(label, hint="enter confirms, esc cancels", extra=""):
            """A card number; a key from `extra`; None for esc or an empty enter."""
            got = read_buf(label, hint, str.isdigit, extra, mark="#")
            if isinstance(got, tuple):
                return got[1]
            return int(got) if got else None

        def selected_issue():
            """The card under the cursor, or None (no cursor, or it went stale)."""
            cur = st["cursor"]
            if cur is None or cur[0] >= len(st["columns"]):
                return None
            shown = st["columns"][cur[0]][1][: st["limit"] or None]
            return shown[cur[1]] if cur[1] < len(shown) else None

        def read_card(label):
            """The card to act on: the one under the cursor, else a typed
            number that is on the board. A card with no number yet (offline,
            `(new)`) is named by its title. None with the reason otherwise."""
            if (issue := selected_issue()) is not None:
                return issue.iid if issue.iid is not None else issue.title
            iid = read_iid(label)
            if iid is not None and live_issue(iid) is None:
                st["status"] = Text(
                    f"#{iid} is not on this board", "logging.level.error"
                )
                return None
            return iid

        def read_line(label, hint="enter confirms, esc cancels"):
            got = read_buf(label, hint, str.isprintable)
            return got.strip() or None if got else None

        def live_issue(iid):
            for _, issues in st["columns"]:
                for issue in issues:
                    if issue.iid == iid:
                        return issue
            return None

        def issue_title(iid):
            issue = live_issue(iid)
            return issue.title if issue else None

        def pick(prompt, options, panel_title, current=None, extra=""):
            """The numbered overlay, one keypress: an index, a key from
            `extra`, or None."""
            # ponytail: single-digit pick caps at 9; past that, type it (t)
            # or pass the project/board arguments instead
            options = options[:9]
            grid = Table(box=None, show_header=False, padding=(0, 1))
            grid.add_column(style="bold reverse", width=3)
            grid.add_column()
            for i, text in enumerate(options, 1):
                here = "  ← current" if text == current else ""
                grid.add_row(f" {i} ", f"{text}{here}")
            st["extra"] = Panel(
                grid, title=panel_title, border_style="muted", padding=(0, 1)
            )
            st["prompt"] = f"{prompt}  (anything else cancels)"
            draw()
            st["prompt"], st["extra"] = None, None
            k = _key()
            if k.isdigit() and 1 <= int(k) <= len(options):
                return int(k) - 1
            if extra and k.lower() in extra:
                return k.lower()
            return None

        def spec_file():
            """The board's YAML, pulled from the board when there is none yet."""
            if not st["spec"]:
                spec = f"boards/{st['path'].rsplit('/', 1)[-1]}.yaml"
                _pull_spec(st["proj"], st["board"], st["columns"], spec)
                st["spec"] = spec
            return st["spec"]

        def raw_spec(path):
            """The YAML as written — `load` validates, but it also rewrites
            colour names to hex, and a staged move should not touch those."""
            import yaml

            apply_mod.load(path)
            spec = yaml.safe_load(Path(path).read_text())
            spec.setdefault("issues", [])
            return spec

        def staged_panel():
            body = Text("\n".join(st["staged"][-8:]))
            title = f"staged this session ({len(st['staged'])})"
            return Panel(body, title=title, border_style="muted", padding=(0, 1))

        def stage(iid, fn, *args):
            """Run one edit.py action on the YAML and write it; never GitLab."""
            path = spec_file()
            spec = raw_spec(path)
            if isinstance(iid, int) and edit_mod.find(spec, iid) is None:
                issue = live_issue(iid)
                if issue is not None and not offline:
                    edit_mod.adopt(spec, apply_mod.issue_entry(issue))
            try:
                line = fn(spec, *args)
            except edit_mod.EditError as e:
                st["status"] = Text(str(e), "logging.level.error")
                return
            Path(path).write_text(apply_mod.dump(spec))
            st["staged"].append(line)
            if offline:
                refetch()
            how = "the host pushes" if offline else "a pushes"
            st["status"] = Text(f"staged: {line} · p shows, {how}", "added")
            st["extra"] = staged_panel()

        def card_facts(iid):
            """(assignee, labels) as staged in the YAML, else as on the board."""
            entry = edit_mod.find(raw_spec(spec_file()), iid)
            if entry is not None:
                return entry.get("assignee"), entry.get("labels") or []
            issue = live_issue(iid)
            if issue is None:
                return None, []
            who = issue.assignee["username"] if issue.assignee else None
            return who, list(issue.labels or [])

        def estimated_due(iid):
            """`+N` from the assignee's history, or None with the reason shown."""
            if offline:
                st["status"] = Text("offline — no history here; type a date", "muted")
                return None
            if "history" not in st:
                draw(busy="reading the finished history…")
                st["history"] = _history(
                    st["path"], st["name"], estimate_mod.HISTORY_DAYS // 2
                )[0]
            cfg = estimate_mod.config(raw_spec(spec_file()))
            who, labels = card_facts(iid)
            est = estimate_mod.estimate(
                who,
                labels,
                estimate_mod.samples(st["history"]),
                cfg["method"],
                cfg["min_samples"],
            )
            if est is None:
                st["status"] = Text(
                    "no estimate: too little finished history — type a date", "muted"
                )
                return None
            st["basis"] = est["basis"]
            return f"+{est['days']}"

        def pick_board(title="which board?"):
            """The numbered board overlay; (path, name) or None."""
            choices = board_choices()[:9]
            if len(choices) < 2:
                st["status"] = Text("nothing else to switch to", "muted")
                return None
            names = [f"{proj_path} — {name}" for proj_path, name in choices]
            got = pick(
                title, names, "boards", current=f"{st['path']} — {st['board'].name}"
            )
            return choices[got] if got is not None else None

        def help_panel():
            lines = [
                ("", "The YAML in boards/ is the source of truth; the board is"),
                ("", "what GitLab currently shows. The card keys stage changes"),
                ("", "into the YAML for you; a pushes them to GitLab."),
                ("", "--from FILE: offline. No b/s/m/a/y/f; p diffs against"),
                ("", "FILE.base; copy the YAML to the host and push there."),
                ("r", "refetch the board"),
                ("b", "switch board — this project's, plus any that a"),
                ("", "boards/*.yaml defines (other projects included)"),
                ("s", "append every issue to snapshots.jsonl, the progress log"),
                ("e", "edit the YAML in $EDITOR (pulled from the board if there"),
                ("", "is none yet); the diff is shown when you come back"),
                ("p", "diff the YAML against the board — never writes"),
                ("a", "push the YAML to the board — additive only, y/n first"),
                ("y", "sync: push, snapshot, then refresh the YAML from GitLab"),
                ("f", "pull: replace the YAML with the live board (asks first)"),
                ("m", "copy a finished issue's comments onto one or more"),
                ("", "successors — enter on an empty prompt runs it"),
                ("↑↓", "select a card (arrows or h j k l); the card keys then act"),
                ("", "on it with no number to type. esc drops the selection"),
                ("v", "move a card: number, then a column (not out of Verify,"),
                ("", "not into Done/Failed — those are verdict comments)"),
                ("u", "assign a card: number, then a person, or t to type one"),
                ("d", "due date: number, then YYYY-MM-DD, +N days, or e for the"),
                ("", "estimate from the assignee's finished history"),
                ("c", "stage a comment on a card; a pushes it"),
                ("n", "new card: a title, then a column"),
                ("g", "show or hide the guide panels"),
                ("P", "perch, B Budgie: switch app (when opened from perch tui)"),
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
                live.update(view() if "proj" in st else no_board(), refresh=True)
                if busy:
                    st["status"] = None

            def no_board():
                """GitLab never answered: why, and the keys that still work."""
                return Group(
                    Text("  ") + (st["status"] or Text("")),
                    Text("  r retry · P perch · B Budgie · q quit", "muted"),
                )

            def attempt(fn, *args):
                """Run fn; a GitLab or config error goes to the status line."""
                try:
                    fn(*args)
                except (client.GitlabProblem, apply_mod.SpecError, ConfigError) as e:
                    st["status"] = st["error"] = Text(
                        f"{e} · r retry", "logging.level.error"
                    )

            # a resize re-renders at the new size, including the fit limit
            signal.signal(signal.SIGWINCH, lambda *_: draw())

            def act(k):
                """One key's action, the board loaded."""
                if k in ("up", "down", "left", "right"):
                    sizes = [
                        len(issues[: st["limit"] or None])
                        for _, issues in st["columns"]
                    ]
                    st["cursor"] = _move_cursor(st["cursor"], k, sizes)
                elif k == "\x1b":
                    st["cursor"] = None
                elif k == "g":
                    st["guide"] = not st["guide"]
                    st["status"] = Text(
                        "guide on — press a key to see its panel"
                        if st["guide"]
                        else "guide off — g brings it back",
                        "muted",
                    )
                elif offline and k in "sbmayf":
                    st["tip"] = None
                    st["status"] = Text(
                        "offline — not here; the host does that", "muted"
                    )
                elif k == "r":
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
                    picked = pick_board()
                    if picked:
                        was = st["path"], st["name"]
                        st["path"], st["name"] = picked
                        draw(busy=f"reading {st['path']}…")
                        try:
                            refetch()
                        except BaseException:
                            # the old board is still shown; r must retry it
                            st["path"], st["name"] = was
                            raise
                    else:
                        st["status"] = st["status"] or Text("cancelled", "muted")
                elif k == "e":
                    spec = spec_file()
                    live.stop()
                    editor = shlex.split(
                        os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vim"
                    )
                    subprocess.call([*editor, spec])
                    live.start(refresh=True)
                    parsed = apply_mod.load(spec)
                    draw(busy="comparing…")
                    if offline:
                        refetch()
                    pending = staged(parsed)
                    if pending is None:
                        st["status"] = Text(
                            f"edited; no {base} to diff against", "muted"
                        )
                    elif pending:
                        title = (
                            f"{spec} — {'the host pushes' if offline else 'a pushes'}"
                        )
                        st["extra"] = _changes_table(pending, title)
                    else:
                        st["status"] = Text(
                            "no changes — board already matches", "muted"
                        )
                elif k == "v":
                    iid = read_card("move")
                    names = [edit_mod.BACKLOG] + [
                        n
                        for n, _ in st["columns"]
                        if n != edit_mod.BACKLOG and n not in edit_mod.VERDICT_ONLY
                    ]
                    got = (
                        None
                        if iid is None
                        else pick(f"{edit_mod.name(iid)} → ?", names, "columns")
                    )
                    if got is None:
                        st["status"] = st["status"] or Text("cancelled", "muted")
                    else:
                        stage(iid, edit_mod.move, iid, names[got])
                elif k == "u":
                    iid = read_card("assign")
                    who = None
                    if iid is not None:
                        people = {
                            i.assignee["username"]
                            for _, issues in st["columns"]
                            for i in issues
                            if i.assignee
                        }
                        people |= set(
                            (raw_spec(spec_file()).get("people") or {}).values()
                        )
                        names = sorted(people)
                        got = pick(
                            f"{edit_mod.name(iid)} → who?  (t types a username)",
                            names,
                            "people",
                            extra="t",
                        )
                        if got == "t":
                            who = read_line(f"{edit_mod.name(iid)} → username")
                        elif got is not None:
                            who = names[got]
                    if who is None:
                        st["status"] = st["status"] or Text("cancelled", "muted")
                    else:
                        stage(iid, edit_mod.assign, iid, who)
                elif k == "d":
                    iid = read_card("due date for")
                    text = None
                    st["basis"] = None
                    if iid is not None:
                        text = read_buf(
                            f"{edit_mod.name(iid)} due",
                            "YYYY-MM-DD or +N days, e estimates, esc cancels",
                            str.isprintable,
                            extra="e",
                        )
                        if isinstance(text, tuple):
                            text = estimated_due(iid)
                    if text:
                        today = datetime.now(UTC).date()
                        stage(iid, edit_mod.set_due, iid, text, today)
                        if st["basis"] and st["staged"]:
                            st["status"].append(f" · {st['basis']}", "muted")
                    elif st["status"] is None:
                        st["status"] = Text("cancelled", "muted")
                elif k == "c":
                    iid = read_card("comment on")
                    body = (
                        None
                        if iid is None
                        else read_line(f"{edit_mod.name(iid)} comment")
                    )
                    if body is None:
                        st["status"] = st["status"] or Text("cancelled", "muted")
                    else:
                        stage(iid, edit_mod.add_note, iid, body)
                elif k == "n":
                    title = read_line("new card title")
                    names = [edit_mod.BACKLOG] + [
                        n
                        for n, _ in st["columns"]
                        if n != edit_mod.BACKLOG and n not in edit_mod.VERDICT_ONLY
                    ]
                    got = (
                        None
                        if title is None
                        else pick(f"{title} → ?", names, "columns")
                    )
                    if got is None:
                        st["status"] = Text("cancelled", "muted")
                    else:
                        stage(None, edit_mod.new_card, title, names[got])
                elif k == "m":
                    m_src = read_iid("copy comments from")
                    dsts = []
                    if m_src is not None:
                        title = issue_title(m_src)
                        source = f"#{m_src}"
                        if title:
                            source += f" “{title[:40]}”"
                        target = st["path"]
                        while True:
                            got = "" if not dsts else f" (have {len(dsts)})"
                            label = f"{source}  →  onto"
                            if target != st["path"]:
                                label += f" {target}"
                            nxt = read_iid(
                                f"{label}{got}",
                                hint="enter runs, b picks a project, esc cancels",
                                extra="b",
                            )
                            if nxt == "b":
                                picked = pick_board("destination project?")
                                if picked:
                                    target = picked[0]
                                continue
                            if nxt is None:
                                break
                            dsts.append((target, nxt))
                    if not dsts:
                        st["status"] = Text("cancelled", "muted")
                    else:
                        total = 0
                        wheres = []
                        gl = client.gitlab(write=True)
                        with client.write_errors():
                            for m_path, m_dst in dsts:
                                ref = (
                                    f"#{m_dst}"
                                    if m_path == st["path"]
                                    else f"{m_path}#{m_dst}"
                                )
                                wheres.append(ref)
                                draw(busy=f"copying #{m_src} -> {ref}…")
                                total += apply_mod.migrate_comments(
                                    gl, st["path"], m_src, m_dst, dst_path=m_path
                                )
                        where = ", ".join(wheres)
                        st["status"] = Text(
                            f"{total} comment(s) copied #{m_src} -> {where}", "added"
                        )
                        st["prompt"] = f"close #{m_src} as superseded?  y / n"
                        draw()
                        st["prompt"] = None
                        if _key().lower() == "y":
                            with client.write_errors():
                                apply_mod.close_issue(
                                    gl, st["path"], m_src, superseded_by=wheres
                                )
                            draw(busy="closing…")
                            refetch()
                            st["status"] = Text(
                                f"{total} comment(s) copied; #{m_src} closed", "added"
                            )
                elif k == "f" and st["spec"]:
                    spec = st["spec"]
                    existing = apply_mod.load(spec)
                    lost = _staged_edits(spec, existing)
                    if lost:
                        st["extra"] = _changes_table(
                            lost,
                            f"pull overwrites {spec} with the live board. "
                            f"These {len(lost)} staged edits will be lost:",
                        )
                        st["prompt"] = "pull and lose them?  y / n"
                    elif not Path(f"{spec}.base").exists():
                        st["prompt"] = (
                            f"pull overwrites {spec}; with no .base to compare, "
                            "any edits in it are lost.  y / n"
                        )
                    else:
                        st["prompt"] = (
                            f"pull overwrites {spec}; nothing staged is lost.  y / n"
                        )
                    draw()
                    st["prompt"] = None
                    if _key().lower() == "y":
                        draw(busy="pulling…")
                        _pull_board(
                            st["path"],
                            st["name"],
                            spec,
                            True,
                            True,
                            Path(f"{spec}.base").exists(),
                            any("discussion" in i for i in existing["issues"]),
                            False,
                        )
                        refetch()
                        st["extra"] = None
                        st["staged"] = []
                        st["status"] = Text(
                            f"pulled {spec} from the live board", "added"
                        )
                    else:
                        st["extra"] = None
                        st["status"] = Text("not pulled — nothing written", "muted")
                elif k in ("p", "a", "y") and st["spec"]:
                    parsed = apply_mod.load(st["spec"])
                    draw(busy="comparing…")
                    pending = staged(parsed, write=(k != "p"))
                    spec = st["spec"]
                    if pending is None:
                        st["status"] = Text(f"no {base} to diff against", "muted")
                    elif not pending:
                        st["status"] = Text(
                            "no changes — board already matches", "muted"
                        )
                        if k == "y":
                            draw(busy="refreshing…")
                            _refresh_spec(spec, parsed, _base_of(spec))
                            refetch()
                            st["staged"] = []
                            st["status"] = Text(
                                f"refreshed {spec} from GitLab", "added"
                            )
                    elif k == "p":
                        how = "the host pushes" if offline else "a pushes"
                        st["extra"] = _changes_table(pending, f"{spec} — {how}")
                    elif refusal := _drift_refusal(pending):
                        st["extra"] = _changes_table(pending, f"{spec} — refused")
                        st["status"] = Text(refusal, "bold red")
                    else:
                        st["extra"] = _changes_table(pending, f"{spec} — will write")
                        st["prompt"] = f"push {len(pending)} change(s)?  y / n"
                        draw()
                        st["prompt"] = None
                        if _key().lower() == "y":
                            draw(busy="writing…")
                            with client.write_errors():
                                gl = client.gitlab(write=True)
                                changes = apply_mod.apply(
                                    gl, parsed, base=_base_of(spec)
                                )
                            refetch()
                            done = f"{len(changes)} change(s) written"
                            if k == "y":
                                _write_snapshot(st["proj"], st["board"])
                                _refresh_spec(
                                    spec,
                                    parsed,
                                    _base_of(spec),
                                    st["proj"],
                                    st["board"],
                                )
                                refetch()
                                done += f"; {spec} refreshed from GitLab"
                            st["extra"] = None
                            st["staged"] = []
                            st["status"] = Text(done, "added")
                        else:
                            st["extra"] = None
                            st["status"] = Text("not pushed", "muted")

            live.update(Text(f"reading {st['path']}…", "muted"), refresh=True)
            attempt(refetch)
            draw()
            while True:
                raw = _key()
                k = raw.lower()
                st["status"] = st["extra"] = st["prompt"] = None
                if raw in SWITCH:  # before lowercasing turns P into plan
                    st["tip"] = None  # perch-dvq item 2: no stale guide tip
                    entry = _suite_entry(SWITCH[raw])
                    if entry and not _in_suite():
                        return SWITCH[raw]
                    if entry:  # perch suite: hop, keep the board running
                        why = _hop(SWITCH[raw], entry)
                        if why:
                            st["status"] = Text(
                                f"switch failed: {why}", "logging.level.error"
                            )
                    else:
                        st["status"] = Text(NO_SUITE, "muted")
                    draw()
                    continue
                st["tip"] = guide_mod.panel(k) if st["guide"] else None
                if k == "q":
                    return
                k = {"h": "left", "j": "down", "k": "up", "l": "right"}.get(k, k)
                if "proj" in st or k == "r":
                    attempt(act, k)
                else:  # nothing else works without a board
                    st["status"] = st["error"]
                draw()

    target = _run(go)
    if target:
        _switch(_suite_entry(target))


def _review_first(row):
    """Notes, then link removals and reorders: what the lead reads before a
    go-ahead goes above the routine label and date churn."""
    _, what, detail = row
    if what == "note":
        return 0
    return 1 if what in ("link", "order") or ": blocked_by " in detail else 2


def _changes_table(pending, title):
    table = Table(title=title, title_justify="left", title_style="muted", box=None)
    table.add_column("", width=1)
    table.add_column("", style="muted", width=7)
    table.add_column("")
    for kind, what, detail in sorted(pending, key=_review_first):
        # Text, not markup: `[#11]` (a blocked_by value) is a rich colour tag
        table.add_row(
            Text(SIGN.get(kind, "?"), STYLE.get(kind, "")), Text(what), Text(detail)
        )
    return table


def _print_changes(pending, title):
    if not pending:
        err().print("[muted]no changes — board already matches[/]")
        return
    err().print(_changes_table(pending, title))


def _asks(body):
    """True if a note's first line (past the staged marker) starts with `Q:`."""
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if lines and lines[0].startswith(apply_mod.MARKER):
        lines = lines[1:]
    return bool(lines) and lines[0].lstrip().startswith(stats_mod.QUESTION)


def waiting_questions(spec_issue):
    """`Q:` entries in the discussion or staged notes nobody else answered since.

    Staged notes come after the pulled discussion and count as their own
    author, so a staged reply answers a pulled question.
    """
    entries = [(d["by"], d["body"]) for d in spec_issue.get("discussion") or []]
    entries += [(None, b) for b in apply_mod.staged_notes(spec_issue)]
    return sum(
        1
        for k, (who, body) in enumerate(entries)
        if _asks(body) and all(a == who for a, _ in entries[k + 1 :])
    )


@app.command()
def status(
    db: str = typer.Option(SNAPSHOTS, "--db", help="Snapshot log to read."),
):
    """Every local board YAML at a glance. No network.

    pulled: age of <spec>.base. staged: what plan would write, minus notes.
    notes: staged replies not yet in the discussion. questions: `Q:` notes
    still waiting (- when the YAML was pulled without --notes). verify: the
    oldest issue waiting in Verify, from the snapshot log. overdue: past-due
    issues in the YAML. snapshot: age of the last log line for the project.
    """

    def go():
        now = datetime.now(UTC)
        today = now.date().isoformat()
        rows = []
        for path, spec in local_specs():
            base_file = Path(f"{path}.base")
            staged = pulled = None
            if base_file.exists():
                pulled = datetime.fromtimestamp(base_file.stat().st_mtime, UTC)
                have = apply_mod.have_from_spec(apply_mod.load(str(base_file)))
                staged = sum(
                    1 for _, what, _ in apply_mod.diff(spec, have) if what != "note"
                )
            notes = sum(
                1
                for i in spec["issues"]
                for b in apply_mod.staged_notes(i)
                if b
                not in {
                    apply_mod.norm_text(d["body"]) for d in i.get("discussion") or []
                }
            )
            questions = None
            if any("discussion" in i for i in spec["issues"]):
                questions = sum(waiting_questions(i) for i in spec["issues"])
            batches = report_mod.load(db, project=spec["project"], days=AGE_WINDOW)
            ages = report_mod.age_days(report_mod.column_ages(batches), now)
            verify = [d for col, d in ages.values() if "Verify" in col.split("+")]
            overdue = sum(
                1
                for i in spec["issues"]
                if i.get("due_date") and str(i["due_date"]) < today
            )
            last = None
            if batches:
                last = datetime.fromisoformat(next(iter(batches[-1].values()))["ts"])
            rows.append(
                (
                    f"{spec['project']} · {spec['board']}",
                    pulled,
                    staged,
                    notes,
                    questions,
                    verify,
                    overdue,
                    last,
                )
            )

        rows.sort(key=lambda r: -1 if r[2] is None else -r[2])
        table = Table(box=None)
        table.add_column("board", style="bold")
        table.add_column("pulled", style="muted")
        for name in ("staged", "notes", "questions", "verify", "overdue"):
            table.add_column(name, justify="right")
        table.add_column("snapshot", style="muted")
        for name, pulled, staged, notes, questions, verify, overdue, last in rows:
            table.add_row(
                name,
                _ago(pulled, now) if pulled else "never",
                "-" if staged is None else f"[changed]{staged}[/]" if staged else "0",
                f"[added]{notes}[/]" if notes else "0",
                "-"
                if questions is None
                else f"[bold red]{questions}[/]"
                if questions
                else "0",
                f"[bold red]{max(verify)}d[/]" if verify else "-",
                f"[bold red]{overdue}[/]" if overdue else "0",
                _ago(last, now) if last else "never",
            )
        if not rows:
            err().print("[muted]no boards/*.yaml here — `gitboard pull` one[/]")
            return
        out().print(table)

    _run(go)


@app.command()
def config():
    """Show the resolved configuration and where the token would come from.

    Exits 1 when no read token is found, after printing the table, so a caller
    such as `perch doctor` can report it.
    """
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
        has_token = True
    except ConfigError:
        table.add_row("read token", "[logging.level.error]not found[/]")
        has_token = False
    if cfg.write_token_source:
        table.add_row(
            "write token", f"[added]found[/] [muted]({cfg.write_token_source})[/]"
        )
    else:
        table.add_row("write token", "[muted]unset — push reuses the read token[/]")
    out().print(table)
    if not has_token:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
