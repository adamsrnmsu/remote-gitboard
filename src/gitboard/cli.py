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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.table import Table
from rich.text import Text

from gitboard import apply as apply_mod
from gitboard import board as board_mod
from gitboard import client
from gitboard import ingest as ingest_mod
from gitboard import mail as mail_mod
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

SIGN = {"added": "+", "changed": "~", "skipped": "-", "drift": "!"}
STYLE = {
    "added": "added",
    "changed": "changed",
    "skipped": "muted",
    "drift": "bold red",
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
    """Show what apply would change. Never writes."""

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
    if not yes and not typer.confirm(f"apply {len(writes)} change(s)?"):
        raise typer.Abort()
    return writes


def _write_spec(parsed, base, yes, ignore_drift):
    """plan -> table -> y/n -> apply -> snapshot: the core of apply and land.

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


@app.command()
def apply(
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
def land(
    spec: str | None = typer.Argument(None, help="Path to a board YAML file."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
    ignore_drift: bool = IGNORE_DRIFT,
):
    """apply, snapshot, then refresh the YAML and <spec>.base from GitLab.

    The host's end-of-round step: what the container staged is written, the
    log gets a line, and both files move forward to the live board so the
    next `status`, `plan` and `pull` measure from now. Posted `notes:` come
    back as `discussion:`; a field GitLab kept (skipped) comes back as
    GitLab has it. Nothing is staged after a land, by construction.
    """

    def go():
        spec_path = _need(spec, "spec", "spec file")
        parsed = apply_mod.load(spec_path)
        base = _base_of(spec_path)
        _, proj, board = _write_spec(parsed, base, yes, ignore_drift)
        if proj is None:
            proj, board = board_mod.fetch(parsed["project"], parsed["board"])
        columns = board_mod.board_columns(proj, board)
        notes = any("discussion" in i for i in (base or parsed)["issues"])
        _rotate_base(spec_path)
        _pull_spec(proj, board, columns, spec_path, notes=notes, force=True)
        _pull_spec(proj, board, columns, f"{spec_path}.base", notes=notes)
        err().print(
            f"[added]refreshed {spec_path} and its .base[/] from the live board"
        )
        if (Path(spec_path).parent / "issues.jsonl").exists():
            err().print(
                f"[muted]next: bd import {Path(spec_path).parent / 'issues.jsonl'}[/]"
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
        "does not (edits that were never applied).",
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
    """Save the live board as YAML — the file plan/apply read. Reads only."""

    def one(path, name, target, force):
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
            base_file = Path(f"{target}.base")
            if existing and force and base_file.exists() and not discard_edits:
                staged = apply_mod.diff(
                    existing, apply_mod.have_from_spec(apply_mod.load(str(base_file)))
                )
                if staged:
                    raise ConfigError(
                        f"{target} has edits not in {target}.base — apply them "
                        "first, or --discard-edits"
                    )
        with err().status(f"reading {path}…"):
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
):
    """What moved on the board, from the snapshot log. Reads only local files."""

    def since_ts(spec_path):
        base_file = Path(f"{spec_path}.base")
        if not base_file.exists():
            return None
        mtime = datetime.fromtimestamp(base_file.stat().st_mtime, UTC)
        return mtime.isoformat(timespec="seconds")

    def go():
        if all_boards:
            _for_each(
                lambda ps: _report(ps[1]["project"], days, db, repo, since_ts(ps[0])),
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
        _report(path, days, db, repo, ts)

    _run(go)


def _report(path, days, db, repo, since_ts=None):
    """One project's report; `since_ts` (ISO) replaces the --days window."""
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
        _movement(console, batches, days, repo)
    latest = batches[-1]
    hits = report_mod.stuck(report_mod.column_ages(batches))
    if hits:
        console.print("[bold red]stuck[/] [muted]— past the column's threshold[/]")
        for iid, col, age in hits:
            console.print(
                f"  [muted]#{iid}[/] {latest[iid]['title']}  [muted]{col} for {age}d[/]"
            )


def _movement(console, batches, days, repo):
    """The moved/new/closed table and the per-assignee tally."""
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
    if not tally:
        return
    authors = report_mod.commit_counts(repo, days) if repo else {}
    who = Table(box=None)
    who.add_column("assignee", style="bold")
    for col in ("moved", "new", "closed"):
        who.add_column(col, justify="right")
    if repo:
        who.add_column("commits", justify="right")
    for name in sorted(tally, key=lambda n: -sum(tally[n].values())):
        row = [name] + [str(tally[name][c] or "") for c in ("moved", "new", "closed")]
        if repo:
            author = report_mod.match_author(name, authors)
            row.append(str(authors.pop(author)) if author else "?")
        who.add_row(*row)
    console.print(who)
    for (a_name, email), count in sorted(authors.items()):
        console.print(
            f"[muted]{count} commit(s) by {a_name} <{email}> matched no assignee[/]"
        )


def _history(project, board_name, days, from_file=None, dump=None):
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
    history, columns = board_mod.fetch_history(
        proj, board, since=now - timedelta(days=2 * days)
    )
    meta = {
        "project": proj.path_with_namespace,
        "board": board.name,
        "columns": columns,
        "fetched_at": now.isoformat(timespec="seconds"),
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
            print(
                f"# {path} — last {weeks} weeks\n\n" + stats_mod.render_weekly_md(rows)
            )
            return
        path = None if from_file else _need(project, "project", "project")
        history, columns, meta = _history(
            path, board_name or get_config().board, days, from_file, dump
        )
        summary, _ = _summary(history, columns, meta, days)
        if as_json:
            print(json.dumps(summary, default=str, indent=2))
        else:
            weekly = _weekly(meta["project"], meta["board"])
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

        def write(name, text):
            (folder / name).write_text(text)
            written.append(folder / name)

        written, entries = [], []
        write("team.md", stats_mod.render_team_md(summary, weekly=weekly))
        if series is not None:
            write(
                "team.html",
                mail_mod.render_team_html(summary, series, svg=True, weekly=weekly),
            )
            entries.append(
                {"name": "team", "files": {"md": "team.md", "html": "team.html"}}
            )
        for who in sorted(people):
            person = stats_mod.for_person(summary, history, who, now)
            body = stats_mod.render_person_md(person, summary, who)
            write(f"{who}.md", body)
            to = emails.get(who)
            subject = f"[{meta['project']}] week of {week} — {who}"
            files = {"md": f"{who}.md"}
            html = None
            if series is not None:
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
                    ),
                )
                html = mail_mod.render_person_html(
                    person, summary, who, series, weekly=weekly
                )
                files["html"] = f"{who}.html"
            if to:
                write(f"{who}.eml", stats_mod.eml(to, subject, body, sender, now, html))
                files["eml"] = f"{who}.eml"
            entries.append(
                {"name": who, "to": to or "", "subject": subject, "files": files}
            )
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
    from_file: str | None = typer.Option(
        None,
        "--from",
        help="Offline: the YAML is the board. Keys r/e/p/?/q; the diff is against "
        "<file>.base when it exists. Nothing here touches GitLab.",
    ),
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
        offline = from_file
        base = f"{from_file}.base" if from_file else None
        st = {
            "status": None,
            "extra": None,
            "prompt": None,
            "path": from_file or _need(project, "project", "project"),
            "name": board_name or get_config().board,
        }

        def refetch():
            if offline:
                spec = apply_mod.load(offline)
                st["proj"], st["board"] = board_mod.spec_stand_ins(spec)
                st["columns"] = board_mod.columns_from_spec(spec, get_config().url)
                st["spec"] = offline
                st["ages"] = _ages(spec["project"])
                return
            st["proj"], st["board"] = board_mod.fetch(st["path"], st["name"])
            st["columns"] = board_mod.board_columns(st["proj"], st["board"])
            st["spec"] = find_spec(st["path"])
            st["ages"] = _ages(st["proj"].path_with_namespace)

        def staged(parsed, write=False):
            """What apply would do: against GitLab, or offline against .base."""
            if not offline:
                return apply_mod.plan(client.gitlab(write=write), parsed)
            if not Path(base).exists():
                return None
            return apply_mod.diff(
                parsed, apply_mod.have_from_spec(apply_mod.load(base))
            )

        def keybar():
            if st["prompt"]:
                return Text(f"  {st['prompt']}", "bold yellow")
            if offline:
                pairs = [("r", "reload"), ("e", "edit"), ("p", "diff")]
                pairs += [("?", "help"), ("q", "quit")]
            else:
                pairs = [("r", "reload"), ("b", "board"), ("s", "snapshot")]
                pairs += [("e", "edit")]
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
                st["proj"], st["board"], limit=limit, columns=cols, ages=st["ages"]
            )
            spec = st["spec"]
            if offline:
                have_base = "p diffs against it" if Path(base).exists() else "no .base"
                subtitle = (
                    f"offline — {spec} is the board; {have_base}; the host applies"
                )
            elif spec:
                subtitle = f"defined by {spec} — e edits, a applies"
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
            if st["extra"] is not None:
                parts.append(st["extra"])
            if st["status"] is not None:
                parts.append(Text("  ", end="") + st["status"])
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

        def read_iid(label, hint="enter confirms, esc cancels", extra=""):
            """Digits typed into the prompt line, board still on screen.

            A key from `extra` pressed on an empty buffer is returned as-is,
            so callers can offer escapes like b-for-board without a mode.
            """
            buf = ""
            while True:
                st["prompt"] = f"{label} #{buf}_   ({hint})"
                draw()
                k = _key()
                if k in ("\r", "\n"):
                    st["prompt"] = None
                    return int(buf) if buf else None
                if k == "\x1b":
                    st["prompt"] = None
                    return None
                if not buf and k.lower() in extra:
                    st["prompt"] = None
                    return k.lower()
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

        def pick_board(title="which board?"):
            """The numbered board overlay; (path, name) or None."""
            choices = board_choices()[:9]
            if len(choices) < 2:
                st["status"] = Text("nothing else to switch to", "muted")
                return None
            # ponytail: single-digit pick caps at 9; past that, pass the
            # project/board arguments instead
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
            st["prompt"] = f"{title}  (anything else cancels)"
            draw()
            st["prompt"], st["extra"] = None, None
            pick = _key()
            if pick.isdigit() and 1 <= int(pick) <= len(choices):
                return choices[int(pick) - 1]
            return None

        def help_panel():
            lines = [
                ("", "The YAML in boards/ is the source of truth; the board is"),
                ("", "what GitLab currently shows. Editing happens in the YAML."),
                ("", "--from FILE: offline. Only r/e/p/?/q work; p diffs against"),
                ("", "FILE.base; copy the YAML to the host and apply there."),
                ("r", "refetch the board"),
                ("b", "switch board — this project's, plus any that a"),
                ("", "boards/*.yaml defines (other projects included)"),
                ("s", "append every issue to snapshots.jsonl, the progress log"),
                ("e", "edit the YAML in $EDITOR (pulled from the board if there"),
                ("", "is none yet); the diff is shown when you come back"),
                ("p", "diff the YAML against the board — never writes"),
                ("a", "write the YAML to the board — additive only, y/n first"),
                ("m", "copy a finished issue's comments onto one or more"),
                ("", "successors — enter on an empty prompt runs it"),
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
                if offline and k in "sbma":
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
                        st["path"], st["name"] = picked
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
                    if offline:
                        refetch()
                    pending = staged(parsed)
                    if pending is None:
                        st["status"] = Text(
                            f"edited; no {base} to diff against", "muted"
                        )
                    elif pending:
                        title = (
                            f"{spec} — {'the host applies' if offline else 'a applies'}"
                        )
                        st["extra"] = _changes_table(pending, title)
                    else:
                        st["status"] = Text(
                            "no changes — board already matches", "muted"
                        )
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
                elif k in ("p", "a") and st["spec"]:
                    parsed = apply_mod.load(st["spec"])
                    draw(busy="comparing…")
                    pending = staged(parsed, write=(k == "a"))
                    spec = st["spec"]
                    if pending is None:
                        st["status"] = Text(f"no {base} to diff against", "muted")
                    elif not pending:
                        st["status"] = Text(
                            "no changes — board already matches", "muted"
                        )
                    elif k == "p":
                        how = "the host applies" if offline else "a applies"
                        st["extra"] = _changes_table(pending, f"{spec} — {how}")
                    else:
                        st["extra"] = _changes_table(pending, f"{spec} — will write")
                        st["prompt"] = f"apply {len(pending)} change(s)?  y / n"
                        draw()
                        st["prompt"] = None
                        if _key().lower() == "y":
                            draw(busy="writing…")
                            with client.write_errors():
                                gl = client.gitlab(write=True)
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
        table.add_row(Text(SIGN.get(kind, "?"), STYLE.get(kind, "")), what, detail)
    return table


def _print_changes(pending, title):
    if not pending:
        err().print("[muted]no changes — board already matches[/]")
        return
    err().print(_changes_table(pending, title))


@app.command()
def status(
    db: str = typer.Option(SNAPSHOTS, "--db", help="Snapshot log to read."),
):
    """Every local board YAML at a glance. No network.

    pulled: age of <spec>.base. staged: what plan would write, minus notes.
    notes: staged replies not yet in the discussion. verify: the oldest issue
    waiting in Verify, from the snapshot log. overdue: past-due issues in
    the YAML. snapshot: age of the last log line for the project.
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
                    verify,
                    overdue,
                    last,
                )
            )

        rows.sort(key=lambda r: -1 if r[2] is None else -r[2])
        table = Table(box=None)
        table.add_column("board", style="bold")
        table.add_column("pulled", style="muted")
        for name in ("staged", "notes", "verify", "overdue"):
            table.add_column(name, justify="right")
        table.add_column("snapshot", style="muted")
        for name, pulled, staged, notes, verify, overdue, last in rows:
            table.add_row(
                name,
                _ago(pulled, now) if pulled else "never",
                "-" if staged is None else f"[changed]{staged}[/]" if staged else "0",
                f"[added]{notes}[/]" if notes else "0",
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
