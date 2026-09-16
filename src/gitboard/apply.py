"""Make a GitLab board match a YAML file. The only thing here that writes.

Needs an `api`-scope token; the `read_api` token board reading uses will fail
here, which is the intended split.

Idempotent: edit the YAML, re-run, and only the drift is written. Additive
only — nothing is deleted or closed, so removing an issue from the YAML
leaves it on the board. Issues are matched by title.
"""

import re

import yaml

from gitboard import client
from gitboard.log import get_logger

log = get_logger()


class SpecError(Exception):
    """The YAML is wrong. Rendered as one line, no traceback."""


# GitLab's own label palette plus the basic CSS names, so a column can say
# `color: crimson` instead of `#dc143c`. The API only speaks hex, so names
# are translated on load and back on pull.
COLORS = {
    "red": "#ff0000",
    "crimson": "#dc143c",
    "rose red": "#c21e56",
    "magenta pink": "#cc338b",
    "pink": "#ffc0cb",
    "dark coral": "#cd5b45",
    "orange": "#ffa500",
    "carrot orange": "#ed9121",
    "aztec gold": "#c39953",
    "champagne": "#f7e7ce",
    "yellow": "#ffff00",
    "titanium yellow": "#eee600",
    "green": "#008000",
    "green cyan": "#009966",
    "green screen": "#00b140",
    "dark green": "#013220",
    "dark sea green": "#8fbc8f",
    "medium sea green": "#3cb371",
    "teal": "#008080",
    "blue": "#0000ff",
    "gitlab blue": "#428bca",
    "blue gray": "#6699cc",
    "lavender": "#e6e6fa",
    "purple": "#800080",
    "dark violet": "#9400d3",
    "deep violet": "#330066",
    "brown": "#a52a2a",
    "gray": "#808080",
    "grey": "#808080",
    "charcoal": "#36454f",
    "black": "#000000",
    "white": "#ffffff",
}
COLOR_NAMES = {}
for _name, _hex in COLORS.items():
    COLOR_NAMES.setdefault(_hex, _name)


def norm_color(value):
    """A color as the API's lowercase hex, from hex or a friendly name."""
    value = str(value).strip()
    if re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", value):
        return value.lower()
    key = " ".join(value.lower().replace("-", " ").replace("_", " ").split())
    if key in COLORS:
        return COLORS[key]
    raise SpecError(
        f"unknown color {value!r} — use hex like '#428bca', or one of: "
        + ", ".join(sorted(COLORS))
    )


def load(path):
    try:
        with open(path) as f:
            spec = yaml.safe_load(f)
    except FileNotFoundError as e:
        raise SpecError(f"no such board file: {path}") from e
    except OSError as e:
        raise SpecError(f"cannot read {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise SpecError(f"{path}: {e}") from e
    if not isinstance(spec, dict):
        raise SpecError(f"{path}: expected a mapping at the top level")
    for key in ("project", "board"):
        if key not in spec:
            raise SpecError(f"{path}: missing required key {key!r}")
    spec.setdefault("columns", [])
    spec.setdefault("issues", [])
    for col in spec["columns"]:
        if "color" in col:
            col["color"] = norm_color(col["color"])
    return spec


def close_issue(gl, path, iid, superseded_by=()):
    """Close an issue, leaving a note naming what replaced it.

    The forward half of a migration: comments go to the successors, the
    source stops cluttering the board. Already-closed issues are left alone,
    so re-running is a no-op.
    """
    project = client.get_project(gl, path)
    issue = client.get_issue(project, iid)
    if issue.state == "closed":
        return False
    if superseded_by:
        issue.notes.create({"body": "superseded by " + ", ".join(superseded_by)})
    issue.state_event = "close"
    issue.save()
    return True


def spec_from_board(project, board, columns):
    """The live board as an apply()-shaped spec — the pull direction.

    Built so the roundtrip settles: plan() of the result against the same
    board is empty. Backlog is synthesised from unlabelled issues, so it is
    not a column here; empty fields are dropped to keep the YAML editable.
    """
    labels = {x.name: x for x in project.labels.list(all=True)}
    seen = {}
    for _, issues in columns:
        for issue in issues:
            seen[issue.iid] = issue

    spec_issues = []
    for issue in sorted(seen.values(), key=lambda i: i.iid):
        # iid is informational: it lets an offline `show --from` name issues,
        # and plan/apply never read it (identity stays the title).
        entry = {"title": issue.title, "iid": issue.iid}
        if issue.labels:
            entry["labels"] = sorted(issue.labels)
        if body := norm_text(issue.description):
            entry["description"] = body
        if issue.due_date:
            entry["due_date"] = issue.due_date
        if issue.assignee:
            entry["assignee"] = issue.assignee["username"]
        spec_issues.append(entry)

    return {
        "project": project.path_with_namespace,
        "board": board.name,
        "columns": [
            {
                "name": name,
                "color": COLOR_NAMES.get(
                    labels[name].color.lower(), labels[name].color
                ),
            }
            for name, _ in columns
            if name != "Backlog" and name in labels
        ],
        "issues": spec_issues,
    }


def dump(spec):
    """Spec -> YAML text, keys in schema order."""
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=79)


def norm_text(s):
    """GitLab strips trailing whitespace and normalises CRLF; YAML's `|` keeps
    a trailing newline. Without this every apply reports a phantom change."""
    return (s or "").replace("\r\n", "\n").strip()


def wanted_issue(spec):
    """The fields we manage, normalised so they compare cleanly.

    YAML parses an unquoted 2026-09-01 into a date object, which is neither
    JSON-serialisable nor comparable to the ISO string the API returns.
    Assignee is the username here; ids are resolved only when writing, so
    this and plan()/diff() need no network.
    """
    due = spec.get("due_date")
    return {
        "labels": sorted(spec.get("labels", [])),
        "description": norm_text(spec.get("description")),
        "due_date": due.isoformat() if hasattr(due, "isoformat") else due,
        "assignee": spec.get("assignee") or None,
    }


def current_issue(issue):
    # ponytail: first assignee only — a second one is invisible here and a
    # save would drop it. Boards here are single-assignee.
    return {
        "labels": sorted(issue.labels),
        "description": norm_text(issue.description),
        "due_date": issue.due_date,
        "assignee": issue.assignee["username"] if issue.assignee else None,
    }


def resolve_users(gl, spec):
    """{username: id} for every assignee in the spec. Runs before any write
    so a typo'd username fails the whole run, not the middle of it."""
    users = {}
    for issue in spec["issues"]:
        who = issue.get("assignee")
        if who and who not in users:
            found = client.find_user(gl, who)
            if found is None:
                raise SpecError(f"no such user {who!r} (issue {issue['title']!r})")
            users[who] = found.id
    return users


def ensure_project(gl, path, create):
    try:
        return client.get_project(gl, path)
    except client.GitlabProblem:
        if not create:
            raise
        ns, _, name = path.rpartition("/")
        payload = {"name": name, "path": name, "initialize_with_readme": True}
        if ns:
            # Resolve the namespace directly rather than comparing against
            # gl.user.username: gl.user is None until gl.auth() has run, and
            # namespaces.get works for a user's own namespace as well as a
            # group's.
            try:
                payload["namespace_id"] = gl.namespaces.get(ns).id
            except Exception as e:
                raise SpecError(
                    f"cannot create {path!r}: no namespace {ns!r} you can write to"
                ) from e
        log.debug("creating project %s", path)
        return gl.projects.create(payload)


def ensure_labels(project, columns, record):
    have = {x.name: x for x in project.labels.list(all=True)}
    for col in columns:
        name, color = col["name"], col.get("color", "#428bca")
        if name not in have:
            have[name] = project.labels.create({"name": name, "color": color})
            record("added", "label", f"{name} ({color})")
        elif have[name].color.lower() != color.lower():
            have[name].color = color
            have[name].save()
            record("changed", "label", f"{name} colour -> {color}")
    return have


def ensure_board(project, name, columns, labels, record):
    board = next((b for b in project.boards.list(all=True) if b.name == name), None)
    if board is None:
        board = project.boards.create({"name": name})
        record("added", "board", name)
    board = project.boards.get(board.id)

    listed = {
        x.label["name"] for x in board.lists.list(all=True) if getattr(x, "label", None)
    }
    for col in columns:  # YAML order becomes column order
        if col["name"] not in listed:
            board.lists.create({"label_id": labels[col["name"]].id})
            record("added", "column", col["name"])


def _payload(want, users):
    """What the API takes: assignee_ids, not a username."""
    fields = dict(want)
    who = fields.pop("assignee")
    fields["assignee_ids"] = [users[who]] if who else []
    return fields


def ensure_issues(project, issues, record, users):
    have = {i.title: i for i in project.issues.list(state="opened", all=True)}
    for spec in issues:
        title, want = spec["title"], wanted_issue(spec)
        if title not in have:
            project.issues.create({"title": title, **_payload(want, users)})
            record("added", "issue", title)
            continue
        issue = have[title]
        now = current_issue(issue)
        if changed := [k for k, v in want.items() if now[k] != v]:
            for k, v in _payload(want, users).items():
                setattr(issue, k, v)
            issue.save()
            record("changed", "issue", f"{title}: {', '.join(changed)}")


def apply(gl, spec, on_change=None):
    """Write the spec. Returns the list of (kind, what, detail) changes."""
    changes = []

    def record(kind, what, detail):
        changes.append((kind, what, detail))
        # Debug, not info: the CLI already prints the change table, and
        # logging it again at info level says everything twice.
        log.debug("%s %s %s", what, "+" if kind == "added" else "~", detail)
        if on_change:
            on_change(kind, what, detail)

    users = resolve_users(gl, spec)
    project = ensure_project(gl, spec["project"], spec.get("create_project", False))
    labels = ensure_labels(project, spec["columns"], record)
    ensure_board(project, spec["board"], spec["columns"], labels, record)
    ensure_issues(project, spec["issues"], record, users)
    return changes


def plan(gl, spec):
    """Read-only: what apply would do. Never writes."""
    resolve_users(gl, spec)
    try:
        project = client.get_project(gl, spec["project"])
    except client.GitlabProblem:
        return diff(spec, None)
    opened = {i.title: i for i in project.issues.list(state="opened", all=True)}
    have = {
        "labels": {x.name for x in project.labels.list(all=True)},
        "boards": {b.name for b in project.boards.list(all=True)},
        "issues": {title: current_issue(i) for title, i in opened.items()},
    }
    return diff(spec, have)


def have_from_spec(base):
    """A pulled spec as the `have` side of diff() — plan with no network.

    Both sides go through wanted_issue, so dates, trailing newlines and label
    order agree by construction.
    """
    return {
        "labels": {c["name"] for c in base["columns"]},
        "boards": {base["board"]},
        "issues": {i["title"]: wanted_issue(i) for i in base["issues"]},
    }


def diff(spec, have):
    """Pure: the (kind, what, detail) list apply would write. `have` is
    {"labels": set, "boards": set, "issues": {title: current_issue-shaped}},
    or None for a project that does not exist yet."""
    if have is None:
        pending = [("added", "project", spec["project"])]
        pending += [("added", "label", c["name"]) for c in spec["columns"]]
        pending += [("added", "board", spec["board"])]
        pending += [("added", "issue", i["title"]) for i in spec["issues"]]
        return pending

    pending = [
        ("added", "label", c["name"])
        for c in spec["columns"]
        if c["name"] not in have["labels"]
    ]
    if spec["board"] not in have["boards"]:
        pending.append(("added", "board", spec["board"]))
    for spec_i in spec["issues"]:
        title = spec_i["title"]
        if title not in have["issues"]:
            pending.append(("added", "issue", title))
            continue
        want, now = wanted_issue(spec_i), have["issues"][title]
        if changed := [k for k, v in want.items() if now[k] != v]:
            pending.append(("changed", "issue", f"{title}: {', '.join(changed)}"))
    return pending


def migrate_comments(gl, path, src_iid, dst_iid, dst_path=None):
    """Copy one issue's comments onto another, oldest first.

    The API cannot post as someone else, so authorship survives as an
    attribution header instead. Idempotent the same way apply is: a comment
    whose migrated body already sits on the destination is skipped, so
    re-running copies nothing. System notes (relabels, milestone churn) are
    activity, not discussion, and are not copied. `dst_path` sends the copies
    to an issue in another project; the header is then project-qualified,
    because a bare #iid means nothing over there.
    """
    project = client.get_project(gl, path)
    src = client.get_issue(project, src_iid)
    crossing = dst_path not in (None, path)
    dst_project = client.get_project(gl, dst_path) if crossing else project
    dst = client.get_issue(dst_project, dst_iid)
    ref = f"{path}#{src_iid}" if crossing else f"#{src_iid}"

    have = {n.body for n in dst.notes.list(all=True)}
    copied = 0
    for note in sorted(src.notes.list(all=True), key=lambda n: n.created_at):
        # close_issue's breadcrumb is bookkeeping, not discussion
        if note.system or note.body.startswith("superseded by "):
            continue
        body = (
            f"*from {ref}, by @{note.author['username']} on "
            f"{note.created_at[:10]}:*\n\n{note.body}"
        )
        if body in have:
            continue
        dst.notes.create({"body": body})
        copied += 1
    return copied
