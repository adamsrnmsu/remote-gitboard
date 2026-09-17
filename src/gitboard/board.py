"""Read a GitLab issue board. No writes, no CLI — see gitboard.py.

`board_columns` is the reason this repo exists: no MCP server exposes board
structure. A board list is bound to a label and membership is 'has that
label', so the column mapping has to be reassembled from board.lists +
project.issues.
"""

import types
from datetime import date

from rich.console import Group
from rich.text import Text
from rich.tree import Tree

from gitboard import client
from gitboard.log import out


def is_overdue(issue, today=None):
    """ISO dates compare correctly as strings, so no parsing is needed."""
    return bool(issue.due_date) and issue.due_date < (today or date.today().isoformat())


def board_columns(project, board):
    """[(column_name, [issue, ...]), ...] in board order, Backlog first.

    Issues carrying labels from several lists appear in each — that mirrors
    what the GitLab UI does, it does not pick a winner.
    """
    lists = sorted(board.lists.list(all=True), key=lambda x: x.position)
    labelled = [(x.label["name"], x) for x in lists if getattr(x, "label", None)]
    names = [n for n, _ in labelled]

    opened = project.issues.list(state="opened", all=True)
    backlog = [i for i in opened if not (set(i.labels) & set(names))]

    cols = [("Backlog", backlog)]
    cols += [(n, [i for i in opened if n in i.labels]) for n in names]
    return [(name, sorted(issues, key=_urgency)) for name, issues in cols]


def _urgency(issue):
    """Overdue first, then soonest due, then newest.

    Deterministic — the API's own order is not — and it means a truncated
    column shows the issues you would have gone looking for. An issue with
    no iid yet (a spec entry not applied) sorts last, in spec order.
    """
    return (
        not is_overdue(issue),
        issue.due_date or "9999-12-31",
        -(issue.iid or 0),
    )


def ref(issue):
    """`#12`, or `(new)` for a spec entry GitLab has not seen yet."""
    return f"#{issue.iid}" if issue.iid else "(new)"


def columns_from_spec(spec, url):
    """board_columns, but from a pulled YAML — the no-network path.

    Same shape, same Backlog rule, same sort, so every renderer works
    unchanged. Issues are duck-typed; `url` is the GitLab base for the
    per-issue links (only entries carrying an iid get one).
    """
    names = [c["name"] for c in spec["columns"]]
    issues = []
    for entry in spec["issues"]:
        iid = entry.get("iid")
        who = entry.get("assignee")
        due = entry.get("due_date")
        issues.append(
            types.SimpleNamespace(
                iid=iid,
                title=entry["title"],
                labels=list(entry.get("labels", [])),
                description=entry.get("description") or "",
                due_date=due.isoformat() if hasattr(due, "isoformat") else due,
                assignee={"username": who} if who else None,
                web_url=f"{url}/{spec['project']}/-/issues/{iid}" if iid else None,
            )
        )
    backlog = [i for i in issues if not (set(i.labels) & set(names))]
    cols = [("Backlog", backlog)]
    cols += [(n, [i for i in issues if n in i.labels]) for n in names]
    return [(name, sorted(col, key=_urgency)) for name, col in cols]


def spec_stand_ins(spec):
    """(project, board) look-alikes so the renderers need no API objects."""
    return (
        types.SimpleNamespace(path_with_namespace=spec["project"]),
        types.SimpleNamespace(name=spec["board"]),
    )


def fetch(path, board_name=None):
    """(project, board) for a project path, resolving the board by name."""
    gl = client.gitlab()
    project = client.get_project(gl, path)
    boards = project.boards.list(all=True)
    if not boards:
        raise client.GitlabProblem(f"{path} has no issue boards")
    if board_name:
        boards = [b for b in boards if b.name == board_name]
        if not boards:
            have = [b.name for b in project.boards.list(all=True)]
            raise client.GitlabProblem(f"no board named {board_name!r}; have: {have}")
    return project, project.boards.get(boards[0].id)


def as_markdown(project, board, columns=None, ages=None):
    """The stable, parseable rendering. The /board prompt reads this.

    `ages` is `report.age_days(...)`: {iid: (column, days)}. It only ever
    appends ` age:3d` to a line, so older parsers keep working.
    """
    columns = board_columns(project, board) if columns is None else columns
    ages = ages or {}
    lines = [f"# {project.path_with_namespace} — {board.name}\n"]
    for name, issues in columns:
        lines.append(f"## {name} ({len(issues)})\n")
        for i in issues:
            who = i.assignee["username"] if i.assignee else "unassigned"
            extra = [x for x in i.labels if x != name]
            tags = f" `{'` `'.join(extra)}`" if extra else ""
            due = f" due:{i.due_date}" if i.due_date else ""
            age = f" age:{ages[i.iid][1]}d" if i.iid in ages else ""
            lines.append(f"- {ref(i)} {i.title} — @{who}{due}{tags}{age}")
            if i.web_url:
                lines.append(f"  {i.web_url}")
        lines.append("")
    return "\n".join(lines)


def issue_line(issue, column, ages=None):
    """One issue as a styled line. Extra labels are the ones from *other*
    columns — the column's own label is redundant inside it. `ages`
    ({iid: (column, days)}, from report.age_days) adds ` · Verify 3d`."""
    line = Text.assemble(
        (f"{ref(issue)} ", "muted"),
        issue.title,
        (
            f"  @{issue.assignee['username']}"
            if issue.assignee
            else Text("  unassigned", "muted")
        ),
    )
    if issue.due_date:
        overdue = is_overdue(issue)
        line.append(
            f"  {'overdue' if overdue else 'due'} {issue.due_date}",
            "bold red" if overdue else "yellow",
        )
    if extra := [x for x in issue.labels if x != column]:
        line.append(f"  {' '.join(extra)}", "magenta")
    if ages and issue.iid in ages:
        col, days = ages[issue.iid]
        line.append(f" · {col} {days}d", "muted")
    return line


def summarise(columns):
    """Totals over the distinct issues on the board.

    Distinct matters: an issue labelled for two columns appears in both, so
    summing per-column counts would double-count it.
    """
    seen = {}
    for _, issues in columns:
        for issue in issues:
            seen[issue.iid or issue.title] = issue
    issues = list(seen.values())
    return {
        "issues": len(issues),
        "unassigned": sum(1 for i in issues if not i.assignee),
        "overdue": sum(1 for i in issues if is_overdue(i)),
    }


def snapshot_records(project, board, ts):
    """One dict per distinct open issue, ready for a JSONL progress log.

    Distinct by iid for the same reason summarise is; the columns list keeps
    the two-column case visible instead of inventing a winner.
    """
    records = {}
    for name, issues in board_columns(project, board):
        for i in issues:
            rec = records.setdefault(
                i.iid,
                {
                    "ts": ts,
                    "project": project.path_with_namespace,
                    "board": board.name,
                    "iid": i.iid,
                    "title": i.title,
                    "assignee": i.assignee["username"] if i.assignee else None,
                    "due_date": i.due_date,
                    "columns": [],
                },
            )
            rec["columns"].append(name)
    return list(records.values())


def board_view(project, board, limit=5, columns=None, ages=None):
    """Tree + totals as one renderable, plus the hidden count.

    `show` prints it once; `tui` redraws it on every keypress and resize.
    Long columns are truncated: a 200-issue board should still fit on a
    screen, and the point of the overview is shape, not every title.
    `limit=0` renders everything. `columns` skips the refetch when the
    caller already has them; `ages` (see issue_line) adds time-in-column.
    """
    columns = board_columns(project, board) if columns is None else columns
    tree = Tree(
        Text.assemble(
            (project.path_with_namespace, "bold"), " — ", (board.name, "bold cyan")
        ),
        guide_style="muted",
    )
    hidden = 0
    for name, issues in columns:
        flagged = sum(1 for i in issues if is_overdue(i))
        header = Text(f"{name} ({len(issues)})", "col")
        if flagged:
            header.append(f"  {flagged} overdue", "bold red")
        node = tree.add(header)

        shown = issues if limit == 0 else issues[:limit]
        for issue in shown:
            node.add(issue_line(issue, name, ages))
        if not issues:
            node.add(Text("empty", "muted"))
        if rest := len(issues) - len(shown):
            hidden += rest
            node.add(Text(f"… {rest} more", "muted"))

    totals = summarise(columns)
    line = Text()
    line.append(f"{totals['issues']} issues", "bold")
    line.append(f" · {totals['unassigned']} unassigned", "muted")
    if totals["overdue"]:
        line.append(f" · {totals['overdue']} overdue", "bold red")
    return Group(tree, line), hidden


def print_rich(project, board, spec_path=None, limit=5, columns=None, ages=None):
    """The human rendering: the view, then the where-to-edit footer."""
    view, hidden = board_view(project, board, limit, columns=columns, ages=ages)
    console = out()
    console.print()
    console.print(view)

    if hidden:
        console.print(Text(f"{hidden} issue(s) hidden — pass --all", "muted"))
    if spec_path:
        console.print(
            Text.assemble(
                ("defined by ", "muted"),
                (str(spec_path), "cyan"),
                (" — edit it, then `gitboard plan`", "muted"),
            )
        )
    console.print()
