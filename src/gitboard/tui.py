"""`gitboard tui`: the board in a rich Live screen, driven by single keys.

Moved out of cli.py whole (gb-6b2.1); the rules are in src/gitboard/CLAUDE.md
under Rendering rules. Helpers other commands share stay in cli.py.
"""

import codecs
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from rich.table import Table

from gitboard import apply as apply_mod
from gitboard import board as board_mod
from gitboard import client
from gitboard import edit as edit_mod
from gitboard import estimate as estimate_mod
from gitboard import guide as guide_mod
from gitboard import report as report_mod
from gitboard.cli import (
    AGE_WINDOW,
    NO_SUITE,
    SNAPSHOTS,
    SWITCH,
    _ages,
    _base_of,
    _changes_table,
    _drift_refusal,
    _history,
    _hop,
    _in_suite,
    _need,
    _pull_board,
    _pull_spec,
    _refresh_spec,
    _staged,
    _staged_edits,
    _suite_entry,
    _write_snapshot,
    find_spec,
    local_specs,
)
from gitboard.config import ConfigError, get_config
from gitboard.log import err

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


def _key(timeout=None):
    """One keypress. The whole input layer of the TUI. With `timeout`
    (seconds), None when nothing arrives in time (a key already in
    `_pending` is still returned first).

    Reads the fd directly: an arrow arrives as three bytes in one read, and
    `sys.stdin.read(1)` would hand them over as ESC plus two stray letters.
    A paste or fast typing arrives as several keys at once; the rest wait
    in `_pending`.
    """
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    while not _pending:
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return None
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


def run(project, board_name, from_file, no_guide, watch):
    """The TUI loop; returns the suite app to switch to, or None on q."""
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
        "filter": "",
        "guide": get_config().guide and not no_guide,
        "path": from_file or _need(project, "project", "project"),
        "name": board_name or get_config().board,
        "marked": set(),
        "last": None,
    }
    # ponytail: offline polls the file's mtime every 2s, online refetches
    # every --watch minutes; no --watch online means no auto-reload.
    tick = watch * 60 if watch else (2.0 if offline else None)

    def mtime():
        try:
            return os.stat(offline).st_mtime_ns
        except OSError:
            return None

    def refetch(tick=False):
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
        # first load marks nothing; later ones mark what moved since. An
        # idle tick adds to the marks, so a move made while the lead looked
        # away is still marked when they come back; r, a push or a stage
        # starts over.
        moved = board_mod.moved(st["columns"], columns) if "columns" in st else set()
        st["marked"] = (st.get("marked") or set()) | moved if tick else moved
        st.update(proj=proj, board=board, columns=columns, spec=found, ages=ages)
        st["last"] = datetime.now().strftime("%H:%M")
        if offline:
            st["mtime"] = mtime()
        if held is not None:
            st["cursor"] = _find_card(
                shown(), held, st["limit"], prefer=st["cursor"][0]
            )

    def shown():
        """The columns as the user sees them: the / filter applied."""
        return board_mod.filter_columns(st["columns"], st["filter"])

    def staged(parsed, write=False):
        return _staged(parsed, st["spec"], offline, write)

    def keybar():
        if st["prompt"]:
            return Text(f"  {st['prompt']}", "bold yellow")
        bar = Text()
        heads = ("board", "card ")
        for head, pairs in zip(heads, guide_mod.rows(offline, st["spec"]), strict=True):
            bar.append(f"  {head} ", "muted")
            for key, label in pairs:
                bar.append(f" {key} ", "bold reverse")
                bar.append(f" {label}", "muted")
                bar.append("  ")
            bar.append("\n")
        bar.rstrip()
        return bar

    def view():
        cols = shown()
        # ponytail: naive fit — one header + one spare line per column,
        # the rest split evenly. Uneven boards waste a little; fine
        # until someone complains.
        usable = console.size.height - 9 - (2 * len(cols)) - (1 if tick else 0)
        limit = st["limit"] = max(2, usable // max(len(cols), 1))
        cur = st["cursor"]
        body, hidden = board_mod.board_view(
            st["proj"],
            st["board"],
            limit=limit,
            columns=cols,
            ages=st["ages"],
            selected=(cols[cur[0]][0], cur[1]) if selected_issue() else None,
            marked=st["marked"],
        )
        spec = st["spec"]
        if offline:
            have_base = "p diffs against it" if Path(base).exists() else "no .base"
            subtitle = f"offline — {spec} is the board; {have_base}; the host pushes"
        elif spec:
            subtitle = f"defined by {spec} — e edits, a pushes"
        else:
            subtitle = "no YAML yet — e pulls the board into one"
        title = None
        if st["filter"]:
            n = board_mod.summarise(cols)["issues"]
            total = board_mod.summarise(st["columns"])["issues"]
            title = Text(f"filter: {st['filter']} · {n} of {total}", "bold yellow")
        parts = [
            Panel(
                body,
                title=title,
                title_align="left",
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
        if tick and st["last"]:
            every = f"{watch:g}m" if watch else "file change"
            parts.append(
                Text(f"  auto-reload every {every} · last {st['last']}", "muted")
            )
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
        cols = shown()
        if cur is None or cur[0] >= len(cols):
            return None
        visible = cols[cur[0]][1][: st["limit"] or None]
        return visible[cur[1]] if cur[1] < len(visible) else None

    def read_card(label):
        """The card to act on: the one under the cursor, else a typed
        number that is on the board. A card with no number yet (offline,
        `(new)`) is named by its title. None with the reason otherwise."""
        if (issue := selected_issue()) is not None:
            return issue.iid if issue.iid is not None else issue.title
        iid = read_iid(label)
        if iid is not None and live_issue(iid) is None:
            st["status"] = Text(f"#{iid} is not on this board", "logging.level.error")
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
            _pull_spec(st["proj"], st["board"], st["columns"], f"{spec}.base")
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
        got = pick(title, names, "boards", current=f"{st['path']} — {st['board'].name}")
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
            ("a", "push the YAML to the board — y/n first"),
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
            ("/", "filter cards: @user ~label %milestone, other words"),
            ("", "match the title; esc (no selection) clears it"),
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

    def seen_key():
        return f"{st['proj'].path_with_namespace}/{st['board'].name}"

    def mark():
        """Stamp this board as seen now (the next start's 'since')."""
        if "proj" in st:
            report_mod.mark_seen(seen_key())

    def away_line():
        """Status line: what changed since this board was last left."""
        if "proj" not in st:
            return
        since = report_mod.last_seen(seen_key())
        if since is None or not Path(SNAPSHOTS).exists():
            return
        batches = report_mod.load(
            SNAPSHOTS, project=st["proj"].path_with_namespace, days=AGE_WINDOW
        )
        line = report_mod.describe(
            report_mod.away(batches, since.isoformat(), board=st["board"].name),
            since,
        )
        if line:
            st["status"] = Text(line, "muted")

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
                sizes = [len(issues[: st["limit"] or None]) for _, issues in shown()]
                st["cursor"] = _move_cursor(st["cursor"], k, sizes)
            elif k == "\x1b":
                if st["cursor"] is None:
                    st["filter"] = ""
                st["cursor"] = None
            elif k == "/":
                got = read_buf(
                    "filter",
                    "@user ~label %milestone words; empty clears, esc cancels",
                    str.isprintable,
                )
                if got is not None:  # None is esc: keep the filter as it was
                    st["filter"] = got.strip()
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
                st["status"] = Text("offline — not here; the host does that", "muted")
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
                    st["status"] = Text(f"edited; no {base} to diff against", "muted")
                elif pending:
                    title = f"{spec} — {'the host pushes' if offline else 'a pushes'}"
                    st["extra"] = _changes_table(pending, title)
                else:
                    st["status"] = Text("no changes — board already matches", "muted")
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
                    people |= set((raw_spec(spec_file()).get("people") or {}).values())
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
                    None if iid is None else read_line(f"{edit_mod.name(iid)} comment")
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
                got = None if title is None else pick(f"{title} → ?", names, "columns")
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
                        any("discussion" in i for i in existing["issues"]),
                        False,
                    )
                    refetch()
                    st["extra"] = None
                    st["staged"] = []
                    st["status"] = Text(f"pulled {spec} from the live board", "added")
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
                    st["status"] = Text("no changes — board already matches", "muted")
                    if k == "y":
                        draw(busy="refreshing…")
                        _refresh_spec(spec, parsed, _base_of(spec))
                        refetch()
                        st["staged"] = []
                        st["status"] = Text(f"refreshed {spec} from GitLab", "added")
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
                            changes = apply_mod.apply(gl, parsed, base=_base_of(spec))
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
        away_line()
        draw()
        while True:
            raw = _key(tick) if tick else _key()
            if raw is None:  # idle tick: reload, keep what is on screen
                if not offline or mtime() != st.get("mtime"):
                    st["extra"] = None  # a change table would be stale now
                    attempt(refetch, True)
                draw()
                continue
            k = raw.lower()
            st["status"] = st["extra"] = st["prompt"] = None
            if raw in SWITCH:  # before lowercasing turns P into plan
                st["tip"] = None  # perch-dvq item 2: no stale guide tip
                entry = _suite_entry(SWITCH[raw])
                if entry:
                    mark()
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
                mark()
                return
            k = {"h": "left", "j": "down", "k": "up", "l": "right"}.get(k, k)
            if "proj" in st or k == "r":
                attempt(act, k)
            else:  # nothing else works without a board
                st["status"] = st["error"]
            draw()
