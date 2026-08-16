#!/usr/bin/env -S uv run --script
# /// script
# dependencies = ["python-gitlab", "pyyaml"]
# ///
"""Make a GitLab board match a YAML file. Writes — needs an `api`-scope token.

    ./apply.py boards/demo.yaml            # apply
    ./apply.py boards/demo.yaml --dry-run  # show what would change

Idempotent: edit the YAML, re-run, and only the drift is written. Additive
only — nothing is deleted or closed, so removing an issue from the YAML leaves
it on the board. Auth matches board.py (GITLAB_TOKEN, else keychain).
"""

import sys

import yaml

from board import URL, token  # same auth, one implementation


def load(path):
    with open(path) as f:
        spec = yaml.safe_load(f)
    for key in ("project", "board"):
        if key not in spec:
            sys.exit(f"{path}: missing required key {key!r}")
    spec.setdefault("columns", [])
    spec.setdefault("issues", [])
    return spec


def norm_text(s):
    """GitLab strips trailing whitespace and normalises CRLF; YAML's `|` keeps
    a trailing newline. Without this every apply reports a phantom change."""
    return (s or "").replace("\r\n", "\n").strip()


def wanted_issue(gl, spec):
    """The fields we manage, normalised so they compare cleanly.

    YAML parses an unquoted 2026-09-01 into a date object, which is neither
    JSON-serialisable nor comparable to the ISO string the API returns.
    """
    due = spec.get("due_date")
    want = {
        "labels": sorted(spec.get("labels", [])),
        "description": norm_text(spec.get("description")),
        "due_date": due.isoformat() if hasattr(due, "isoformat") else due,
        "assignee_ids": [],
    }
    if who := spec.get("assignee"):
        users = gl.users.list(username=who)
        if not users:
            sys.exit(f"no such user {who!r} (issue {spec['title']!r})")
        want["assignee_ids"] = [users[0].id]
    return want


def current_issue(issue):
    return {
        "labels": sorted(issue.labels),
        "description": norm_text(issue.description),
        "due_date": issue.due_date,
        "assignee_ids": [a["id"] for a in issue.assignees],
    }


def ensure_project(gl, path, create):
    try:
        return gl.projects.get(path)
    except Exception:
        if not create:
            sys.exit(
                f"no project {path!r} on {URL} — create it, or set create_project: true"
            )
        ns, _, name = path.rpartition("/")
        payload = {"name": name, "path": name, "initialize_with_readme": True}
        if ns and ns != gl.user.username:
            payload["namespace_id"] = gl.namespaces.get(ns).id
        return gl.projects.create(payload)


def ensure_labels(project, columns, log):
    have = {x.name: x for x in project.labels.list(all=True)}
    for col in columns:
        name, color = col["name"], col.get("color", "#428bca")
        if name not in have:
            have[name] = project.labels.create({"name": name, "color": color})
            log(f"label   + {name} ({color})")
        elif have[name].color.lower() != color.lower():
            have[name].color = color
            have[name].save()
            log(f"label   ~ {name} colour -> {color}")
    return have


def ensure_board(project, name, columns, labels, log):
    board = next((b for b in project.boards.list(all=True) if b.name == name), None)
    if board is None:
        board = project.boards.create({"name": name})
        log(f"board   + {name}")
    board = project.boards.get(board.id)

    listed = {
        x.label["name"] for x in board.lists.list(all=True) if getattr(x, "label", None)
    }
    for col in columns:  # YAML order becomes column order
        if col["name"] not in listed:
            board.lists.create({"label_id": labels[col["name"]].id})
            log(f"column  + {col['name']}")


def ensure_issues(gl, project, issues, log):
    have = {i.title: i for i in project.issues.list(state="opened", all=True)}
    for spec in issues:
        title, want = spec["title"], wanted_issue(gl, spec)
        if title not in have:
            project.issues.create({"title": title, **want})
            log(f"issue   + {title}")
            continue
        issue = have[title]
        now = current_issue(issue)
        if changed := [k for k, v in want.items() if now[k] != v]:
            for k in changed:
                setattr(issue, k, want[k])
            issue.save()
            log(f"issue   ~ {title}: {', '.join(changed)}")


def apply(gl, spec):
    log_lines = []

    def log(msg):
        log_lines.append(msg)
        print(msg)

    project = ensure_project(gl, spec["project"], spec.get("create_project", False))
    labels = ensure_labels(project, spec["columns"], log)
    ensure_board(project, spec["board"], spec["columns"], labels, log)
    ensure_issues(gl, project, spec["issues"], log)
    return log_lines


def plan(gl, spec):
    """Read-only: what apply would do. Never writes."""
    try:
        project = gl.projects.get(spec["project"])
    except Exception:
        project = None

    if project is None:
        pending = [f"project + {spec['project']}"]
        pending += [f"label   + {c['name']}" for c in spec["columns"]]
        pending += [f"board   + {spec['board']}"]
        pending += [f"issue   + {i['title']}" for i in spec["issues"]]
        return pending

    labels = {x.name for x in project.labels.list(all=True)}
    boards = {b.name for b in project.boards.list(all=True)}
    issues = {i.title: i for i in project.issues.list(state="opened", all=True)}

    pending = [
        f"label   + {c['name']}" for c in spec["columns"] if c["name"] not in labels
    ]
    if spec["board"] not in boards:
        pending.append(f"board   + {spec['board']}")
    for spec_i in spec["issues"]:
        title = spec_i["title"]
        if title not in issues:
            pending.append(f"issue   + {title}")
            continue
        want, now = wanted_issue(gl, spec_i), current_issue(issues[title])
        if changed := [k for k, v in want.items() if now[k] != v]:
            pending.append(f"issue   ~ {title}: {', '.join(changed)}")
    return pending


def main(path, dry_run=False):
    import gitlab

    spec = load(path)
    gl = gitlab.Gitlab(URL, private_token=token())

    if dry_run:
        print(f"dry run against {URL} — nothing will be written\n")
        pending = plan(gl, spec)
        print("\n".join(pending) or "no changes — board already matches")
        print(f"\n{len(pending)} change(s) pending")
        return

    changes = apply(gl, spec)
    print(f"\n{len(changes)} change(s) — ./board.py {spec['project']}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        sys.exit(__doc__)
    main(args[0], dry_run="--dry-run" in sys.argv)
