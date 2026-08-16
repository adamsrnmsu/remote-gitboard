#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["python-gitlab"]
# ///
"""Dump a GitLab issue board as markdown. Read-only.

    ./board.py group/project              # first board
    ./board.py group/project "Dev Board"  # named board

Auth: GITLAB_TOKEN env var, else macOS keychain item `gitlab-token`.
Store one with:
    security add-generic-password -a "$USER" -s gitlab-token -w '<PAT>'
"""

import os
import subprocess
import sys

URL = os.environ.get("GITLAB_URL", "https://gitlab.com")


def token():
    if t := os.environ.get("GITLAB_TOKEN"):
        return t
    out = subprocess.run(
        ["security", "find-generic-password", "-s", "gitlab-token", "-w"],
        capture_output=True,
        text=True,
    )
    if out.returncode:
        sys.exit("no token: set GITLAB_TOKEN or add keychain item 'gitlab-token'")
    return out.stdout.strip()


def board_columns(project, board):
    """[(column_name, [issue, ...]), ...] in board order, Backlog first.

    A board list is bound to a label; membership is 'has that label'. Issues
    carrying labels from several lists appear in each — that mirrors what the
    GitLab UI does, it does not pick a winner.
    """
    lists = sorted(board.lists.list(all=True), key=lambda x: x.position)
    labelled = [(x.label["name"], x) for x in lists if getattr(x, "label", None)]
    names = [n for n, _ in labelled]

    opened = project.issues.list(state="opened", all=True)
    backlog = [i for i in opened if not (set(i.labels) & set(names))]

    cols = [("Backlog", backlog)]
    cols += [(n, [i for i in opened if n in i.labels]) for n in names]
    return cols


def render(project, board):
    out = [f"# {project.path_with_namespace} — {board.name}\n"]
    for name, issues in board_columns(project, board):
        out.append(f"## {name} ({len(issues)})\n")
        for i in issues:
            who = i.assignee["username"] if i.assignee else "unassigned"
            extra = [x for x in i.labels if x != name]
            tags = f" `{'` `'.join(extra)}`" if extra else ""
            due = f" due:{i.due_date}" if i.due_date else ""
            out.append(f"- #{i.iid} {i.title} — @{who}{due}{tags}")
            out.append(f"  {i.web_url}")
        out.append("")
    return "\n".join(out)


def main(path, board_name=None):
    import gitlab  # deferred so --selftest runs with no deps installed

    gl = gitlab.Gitlab(URL, private_token=token())
    project = gl.projects.get(path)
    boards = project.boards.list(all=True)
    if not boards:
        sys.exit(f"{path} has no issue boards")
    if board_name:
        boards = [b for b in boards if b.name == board_name] or sys.exit(
            f"no board named {board_name!r}; have: {[b.name for b in boards]}"
        )
    print(render(project, project.boards.get(boards[0].id)))


def _selftest():
    """Column bucketing is the only real logic here. Fake the API surface."""

    # Kept compact: the shape of the fake is the point, expanded it buries the test.
    # fmt: off
    class L:
        def __init__(s, n, p): s.label, s.position = {"name": n}, p
    class Lists:
        def __init__(s, v): s.v = v
        def list(s, **_): return s.v
    class Board:
        def __init__(s, v): s.lists = Lists(v)
    class Issue:
        def __init__(s, iid, labels): s.iid, s.labels = iid, labels
    class Issues:
        def __init__(s, v): s.v = v
        def list(s, **_): return s.v
    class Project:
        def __init__(s, i): s.issues = Issues(i)
    # fmt: on

    issues = [Issue(1, []), Issue(2, ["Doing"]), Issue(3, ["Doing", "Blocked"])]
    # positions deliberately out of order — render must sort by them
    cols = board_columns(Project(issues), Board([L("Blocked", 2), L("Doing", 1)]))

    assert [n for n, _ in cols] == ["Backlog", "Doing", "Blocked"], cols
    assert [i.iid for i in cols[0][1]] == [1], "unlabelled issue belongs in Backlog"
    assert [i.iid for i in cols[1][1]] == [2, 3]
    assert [i.iid for i in cols[2][1]] == [3], "multi-label issue appears in both"
    print("ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    elif len(sys.argv) < 2:
        sys.exit(__doc__)
    else:
        main(*sys.argv[1:3])
