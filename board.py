"""Read a GitLab issue board. No writes, no CLI — see gitboard.py.

`board_columns` is the reason this repo exists: no MCP server exposes board
structure. A board list is bound to a label and membership is 'has that
label', so the column mapping has to be reassembled from board.lists +
project.issues.
"""

from rich.text import Text
from rich.tree import Tree

import client
from log import out


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
    return cols


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


def as_markdown(project, board):
    """The stable, parseable rendering. The /board prompt reads this."""
    lines = [f"# {project.path_with_namespace} — {board.name}\n"]
    for name, issues in board_columns(project, board):
        lines.append(f"## {name} ({len(issues)})\n")
        for i in issues:
            who = i.assignee["username"] if i.assignee else "unassigned"
            extra = [x for x in i.labels if x != name]
            tags = f" `{'` `'.join(extra)}`" if extra else ""
            due = f" due:{i.due_date}" if i.due_date else ""
            lines.append(f"- #{i.iid} {i.title} — @{who}{due}{tags}")
            lines.append(f"  {i.web_url}")
        lines.append("")
    return "\n".join(lines)


def issue_line(issue, column):
    """One issue as a styled line. Extra labels are the ones from *other*
    columns — the column's own label is redundant inside it."""
    line = Text.assemble(
        (f"#{issue.iid} ", "muted"),
        issue.title,
        (
            f"  @{issue.assignee['username']}"
            if issue.assignee
            else Text("  unassigned", "muted")
        ),
    )
    if issue.due_date:
        line.append(f"  due {issue.due_date}", "yellow")
    if extra := [x for x in issue.labels if x != column]:
        line.append(f"  {' '.join(extra)}", "magenta")
    return line


def print_rich(project, board):
    """The human rendering. A tree — no repeated headers, no truncation."""
    tree = Tree(
        Text.assemble(
            (project.path_with_namespace, "bold"), " — ", (board.name, "bold cyan")
        ),
        guide_style="muted",
    )
    for name, issues in board_columns(project, board):
        node = tree.add(Text(f"{name} ({len(issues)})", "col"))
        for issue in issues:
            node.add(issue_line(issue, name))
        if not issues:
            node.add(Text("empty", "muted"))
    console = out()
    console.print()
    console.print(tree)
    console.print()
