"""Make a GitLab board match a YAML file. The only thing here that writes.

Needs an `api`-scope token; the `read_api` token board reading uses will fail
here, which is the intended split.

Idempotent: edit the YAML, re-run, and only the drift is written. Additive
only — nothing is deleted or closed, so removing an issue from the YAML
leaves it on the board. Issues are matched by iid when the entry carries
one, else by title; a title that is closed on GitLab is skipped, never
recreated. With the pulled `.base` alongside, the write is a three-way
merge: what the YAML left alone is never written over a GitLab-side change.
"""

import functools
import re

import yaml
from gitlab.exceptions import GitlabError

from gitboard import client, links, order
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
    column_names = {c["name"] for c in spec["columns"]}
    for label in spec.get("labels") or []:  # optional: absent stays absent
        name = str(label.get("name") or "").strip() if isinstance(label, dict) else ""
        if not name:
            raise SpecError(f"{path}: every entry under labels: needs a name")
        label["name"] = name
        if name in column_names:
            raise SpecError(
                f"{path}: {name!r} is a column — set its colour under columns:, "
                "not labels:"
            )
        if "color" in label:
            label["color"] = norm_color(label["color"])
    milestones = set()
    for m in spec.get("milestones") or []:  # optional, like labels:
        title = str(m.get("title") or "").strip() if isinstance(m, dict) else ""
        if not title:
            raise SpecError(f"{path}: every entry under milestones: needs a title")
        if title in milestones:
            raise SpecError(f"{path}: duplicate milestone {title!r}")
        m["title"] = title
        milestones.add(title)
    seen = set()
    for issue in spec["issues"]:
        title = str(issue.get("title") or "").strip() if isinstance(issue, dict) else ""
        if not title:
            raise SpecError(f"{path}: every issue needs a title")
        issue["title"] = title
        if title.casefold() in seen:
            raise SpecError(f"{path}: duplicate issue title {title!r}")
        seen.add(title.casefold())
        if issue.get("milestone"):
            name = issue["milestone"] = str(issue["milestone"]).strip()
            if name not in milestones:
                raise SpecError(
                    f"{path}: milestone {name!r} (issue {title!r}) is not under "
                    "milestones:"
                )
        if not isinstance(issue.get("blocked_by") or [], list):
            raise SpecError(f"{path}: issue {title!r}: blocked_by must be a list")
        labels = issue.get("labels") or []
        ranks = [x for x in labels if str(x).startswith(links.PRIORITY)]
        if len(ranks) > 1:
            raise SpecError(f"{path}: issue {title!r} has two priority:: labels")
    try:
        links.check(spec)
    except ValueError as e:
        raise SpecError(f"{path}: {e}") from e
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


def discussion(issue):
    """An issue's comments as [{by, at, body}], oldest first, no system notes.

    Pulled into the spec as `discussion:` so the team's feedback travels with
    the board to wherever GitLab is unreachable. apply() never reads it.
    """
    return [
        {
            "by": n.author["username"],
            "at": n.created_at[:10],
            "body": norm_text(n.body),
        }
        for n in sorted(issue.notes.list(all=True), key=lambda n: n.created_at)
        if not n.system
    ]


def issue_entry(issue, blocked_by=None):
    """One live issue as a spec entry; empty fields dropped.

    iid lets an offline `show --from` name issues and lets plan/apply treat
    a retitled entry as a rename rather than a new issue. Also how the TUI
    adopts a card a stale YAML has not seen. `blocked_by` is refs as
    links.read gives them; this project's are written as ints, the way a
    person would type them.
    """
    entry = {"title": issue.title.strip(), "iid": issue.iid}
    if issue.labels:
        entry["labels"] = sorted(issue.labels)
    if body := norm_text(getattr(issue, "description", None)):
        entry["description"] = body
    if issue.due_date:
        entry["due_date"] = issue.due_date
    if issue.assignee:
        entry["assignee"] = issue.assignee["username"]
    if ms := getattr(issue, "milestone", None):
        entry["milestone"] = ms["title"]
    if blocked_by:
        entry["blocked_by"] = [
            int(r) if links.ref_key(r)[0] == 0 else r
            for r in sorted(blocked_by, key=links.ref_key)
        ]
    return entry


def spec_from_board(project, board, columns, notes=False):
    """The live board as an apply()-shaped spec — the pull direction.

    Built so the roundtrip settles: plan() of the result against the same
    board is empty. Backlog is synthesised from unlabelled issues, so it is
    not a column here; empty fields are dropped to keep the YAML editable.
    `notes=True` adds each issue's discussion (one more request per issue).
    Blocker links cost one request per issue; every milestone a card carries
    gets a `milestones:` entry, so the pulled file passes load().
    """
    labels = {x.name: x for x in project.labels.list(all=True)}
    seen = {}
    for _, issues in columns:
        for issue in issues:
            seen[issue.iid] = issue

    spec_issues, milestones = [], {}
    for issue in sorted(seen.values(), key=_board_key):
        refs = [x["ref"] for x in links.read(issue, project)]
        entry = issue_entry(issue, refs)
        if notes and (talk := discussion(issue)):
            entry["discussion"] = talk
        spec_issues.append(entry)
        if ms := getattr(issue, "milestone", None):
            m = {"title": ms["title"]}
            if ms.get("due_date"):
                m["due_date"] = ms["due_date"]
            if desc := norm_text(ms.get("description")):
                m["description"] = desc
            milestones[m["title"]] = m

    spec = {
        "project": project.path_with_namespace,
        "board": board.name,
        "columns": [
            {"name": name, "color": _friendly(labels[name].color)}
            for name, _ in columns
            if name != "Backlog" and name in labels
        ],
    }
    if milestones:
        spec["milestones"] = sorted(
            milestones.values(),
            key=lambda m: (
                m.get("due_date") is None,
                m.get("due_date") or "",
                m["title"],
            ),
        )
    spec["issues"] = spec_issues
    # every non-column label a card carries, with its colour and description,
    # so scoped labels keep their look when the YAML is applied elsewhere
    column_names = {name for name, _ in columns}
    extra = {n for i in seen.values() for n in i.labels} - column_names
    for name in sorted(extra & labels.keys()):
        entry = {"name": name, "color": _friendly(labels[name].color)}
        if desc := norm_text(getattr(labels[name], "description", None)):
            entry["description"] = desc
        spec.setdefault("labels", []).append(entry)
    return spec


def _board_key(issue):
    """GitLab's manual order: relative_position, never-dragged cards last,
    then iid. The YAML's issue order is this, so pull writes it."""
    rp = getattr(issue, "relative_position", None)
    return (rp is None, rp or 0, issue.iid)


def _live_order(project):
    """(open iids in board order, {iid: issue}). Sorted here too: the API
    breaks relative_position ties however it likes."""
    found = project.issues.list(
        state="opened", order_by="relative_position", sort="asc", all=True
    )
    found = sorted((i for i in found if i.state != "closed"), key=_board_key)
    return [i.iid for i in found], {i.iid: i for i in found}


def order_changes(spec, live, base, force=False):
    """Pure: the board order as one field of the three-way merge.

    Returns (records, moves). No base means a hand-written YAML, which never
    reorders. Drift that is exactly a prefix of this same write is where a
    failed apply stopped (GitLab moves one card per call), so a rerun
    finishes it instead of refusing.
    """
    if base is None:
        return [], []
    edited = [i["iid"] for i in spec["issues"] if i.get("iid")]
    old = [i["iid"] for i in base["issues"] if i.get("iid")]
    kind = order.merge(edited, live, old)
    if kind == "drift" and (force or _stopped_partway(edited, live, old)):
        kind = "write"
    if kind == "skipped":
        return [("skipped", "order", "board order changed on GitLab, kept")], []
    if kind == "drift":
        return [("drift", "order", "board order changed on GitLab and in the YAML")], []
    if kind != "write":
        return [], []
    steps = order.moves(edited, live)
    return [
        (
            "changed",
            "order",
            f"#{iid} after #{after}" if after else f"#{iid} to the top",
        )
        for iid, after, _ in steps
    ], steps


def _stopped_partway(edited, live, old):
    # ponytail: replays each prefix, O(moves² · n); moves are a handful of drags
    keep = set(edited) & set(live) & set(old)
    live = [x for x in live if x in keep]
    steps = order.moves(edited, old)
    return any(
        [x for x in order.apply_moves(old, steps[:k]) if x in keep] == live
        for k in range(1, len(steps))
    )


def ensure_order(project, spec, base, record, force=False):
    """Reorder the board to the YAML's issue order, fewest calls first.

    Runs after ensure_issues and re-reads the live order, so the moves are
    computed against the board as it is now; a rerun after a partial
    failure continues from where it stopped.
    """
    if base is None:  # unmanaged: not even the read
        return
    live, by_iid = _live_order(project)
    records, steps = order_changes(spec, live, base, force)
    if not steps:  # skipped, drift, or nothing
        for change in records:
            record(*change)
        return
    for change, (iid, after, before) in zip(records, steps, strict=True):
        try:
            # GitLab names the neighbour, not the side: move_before_id is the
            # card that ends up before this one, move_after_id the one after.
            if after:
                by_iid[iid].reorder(move_before_id=by_iid[after].id)
            else:
                by_iid[iid].reorder(move_after_id=by_iid[before].id)
        except GitlabError as e:
            if getattr(e, "response_code", None) in (401, 403):
                raise  # write_errors names the missing scope
            raise client.GitlabProblem(
                f"reordering #{iid} failed ({e}); the cards before it moved — "
                "re-run apply to continue"
            ) from e
        record(*change)


def _friendly(color):
    """A label colour as pull writes it: the friendly name when there is one."""
    return COLOR_NAMES.get(color.lower(), color)


def dump(spec):
    """Spec -> YAML text, keys in schema order."""
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=79)


def norm_text(s):
    """GitLab strips trailing whitespace and normalises CRLF; YAML's `|` keeps
    a trailing newline. Without this every apply reports a phantom change."""
    return (s or "").replace("\r\n", "\n").strip()


def _iso(value):
    """YAML parses an unquoted 2026-09-01 into a date object, which is neither
    JSON-serialisable nor comparable to the ISO string the API returns."""
    return value.isoformat() if hasattr(value, "isoformat") else value


def wanted_issue(spec, project_path=None, titles=None):
    """The fields we manage, normalised so they compare cleanly.

    Assignee is the username here; ids are resolved only when writing, so
    this and plan()/diff() need no network. `milestone` and `blocked_by` are
    present only when the entry has the key: an absent key is unmanaged.
    `titles` ({title: iid} of the whole spec) resolves title refs.
    """
    want = {
        "labels": sorted(spec.get("labels", [])),
        "description": norm_text(spec.get("description")),
        "due_date": _iso(spec.get("due_date")),
        "assignee": spec.get("assignee") or None,
    }
    if "milestone" in spec:
        want["milestone"] = spec["milestone"] or None
    if "blocked_by" in spec:
        want["blocked_by"] = links.norm_refs(
            spec["blocked_by"], project_path, titles or {}
        )
    return want


def current_issue(issue, blocked_by=None):
    """The live side of wanted_issue. `blocked_by` (refs) is passed only for
    entries that manage it, so other issues cost no links request."""
    # ponytail: first assignee only — a second one is invisible here and a
    # save would drop it. Boards here are single-assignee.
    ms = getattr(issue, "milestone", None)
    have = {
        "labels": sorted(issue.labels),
        "description": norm_text(issue.description),
        "due_date": issue.due_date,
        "assignee": issue.assignee["username"] if issue.assignee else None,
        "milestone": ms["title"] if ms else None,
    }
    if blocked_by is not None:
        have["blocked_by"] = sorted(blocked_by, key=links.ref_key)
    return have


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


DEFAULT_COLOR = "#428bca"


def ensure_labels(project, columns, record, extra=()):
    """Create every column label and `labels:` entry that is missing; fix a
    colour that differs and a description when the spec gives one."""
    have = {x.name: x for x in project.labels.list(all=True)}
    for want in [*columns, *extra]:
        name, color = want["name"], want.get("color", DEFAULT_COLOR)
        desc = want.get("description")
        if name not in have:
            payload = {"name": name, "color": color}
            if desc:
                payload["description"] = desc
            have[name] = project.labels.create(payload)
            record("added", "label", f"{name} ({color})")
            continue
        label, dirty = have[name], False
        if label.color.lower() != color.lower():
            label.color, dirty = color, True
            record("changed", "label", f"{name} colour -> {color}")
        if desc is not None and norm_text(getattr(label, "description", None)) != (
            norm_text(desc)
        ):
            label.description, dirty = desc, True
            record("changed", "label", f"{name} description")
        if dirty:
            label.save()
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


def _live_milestones(gl, project):
    """{title: milestone} of the project and, for a group project, its
    group: a group milestone of the same title counts as existing. The
    project's own win a clash; an unreadable group leaves just those."""
    found = {}
    ns = project.namespace
    if ns["kind"] == "group":
        try:
            group = gl.groups.get(ns["full_path"])
            found = {m.title: m for m in group.milestones.list(all=True)}
        except GitlabError as e:
            log.debug("group milestones of %s: %s", ns["full_path"], e)
    found.update({m.title: m for m in project.milestones.list(all=True)})
    return found


def _milestone_meta(m):
    return {
        "due_date": getattr(m, "due_date", None),
        "description": norm_text(getattr(m, "description", None)),
    }


def _milestone_fields(want):
    """The fields a spec milestone states. An absent due_date or description
    is left alone, as with labels; `due_date: null` clears the date."""
    fields = {}
    if "due_date" in want:
        fields["due_date"] = _iso(want["due_date"])
    if want.get("description") is not None:
        fields["description"] = norm_text(want["description"])
    return fields


def milestone_changes(spec, have):
    """Pure: what ensure_milestones would do. `have["milestones"]` is
    {title: {"due_date", "description"}}. Additive: nothing is closed."""
    known = (have or {}).get("milestones", {})
    pending = []
    for want in spec.get("milestones") or []:
        title = want["title"]
        if title not in known:
            pending.append(("added", "milestone", title))
            continue
        for field, value in _milestone_fields(want).items():
            now = known[title].get(field)
            if value == now:
                continue
            detail = (
                f"{title}: description (edited)"
                if field == "description"
                else f"{title}: due_date {_show(field, now)} -> {_show(field, value)}"
            )
            pending.append(("changed", "milestone", detail))
    return pending


def ensure_milestones(gl, project, spec, record):
    """Create the missing milestones, update the stated fields of the rest.
    Returns {title: id} so cards resolve a title before any write."""
    live = _live_milestones(gl, project)
    for want in spec.get("milestones") or []:
        title, fields = want["title"], _milestone_fields(want)
        if title not in live:
            payload = {"title": title, **{k: v for k, v in fields.items() if v}}
            live[title] = project.milestones.create(payload)
            record("added", "milestone", title)
            continue
        have = {"milestones": {title: _milestone_meta(live[title])}}
        changes = milestone_changes({"milestones": [want]}, have)
        if changes:
            for k, v in fields.items():
                setattr(live[title], k, v)
            live[title].save()
            for change in changes:
                record(*change)
    return {title: m.id for title, m in live.items()}


def _project_id_of(gl):
    """path -> project id for cross-project links, one lookup per path."""
    return functools.cache(lambda path: client.get_project(gl, path).id)


def _payload(fields, users, milestones=None):
    """What the API takes: assignee_ids and milestone_id, not names. Links
    are not issue fields; ensure_issues writes them after every save."""
    fields = dict(fields)
    if "assignee" in fields:
        who = fields.pop("assignee")
        fields["assignee_ids"] = [users[who]] if who else []
    if "milestone" in fields:
        title = fields.pop("milestone")
        if title and title not in (milestones or {}):
            raise SpecError(f"no milestone {title!r} — list it under milestones:")
        fields["milestone_id"] = milestones[title] if title else None
    fields.pop("blocked_by", None)
    return fields


def managed_labels(spec, base=None):
    """The labels the YAML speaks for: column names plus every label an
    issue names. Anything else on a live issue was added in the UI and is
    neither compared nor removed."""
    specs = [spec, base] if base else [spec]
    return {c["name"] for s in specs for c in s["columns"]} | {
        label for s in specs for i in s["issues"] for label in i.get("labels") or []
    }


FIELDS = ("labels", "description", "due_date", "assignee", "milestone", "blocked_by")
# A field an old or live side lacks: a card pulled with no links had none.
_ABSENT = {"blocked_by": []}


def _show(field, value):
    if field == "labels":
        return "[" + ", ".join(value) + "]"
    if field == "blocked_by":
        return "[" + ", ".join(f"#{r}" if r.isdigit() else r for r in value) + "]"
    return "none" if value is None else str(value)


def _detail(title, field, old, new):
    if field == "description":  # never dump bodies
        return f"{title}: description (edited)"
    return f"{title}: {field} {_show(field, old)} -> {_show(field, new)}"


def issue_changes(title, edited, live, old, managed, force=False):
    """Pure three-way merge of one issue's managed fields.

    `edited` is the YAML, `live` the board, `old` the YAML as pulled (None
    when there is no base — then every difference is written). Returns
    ({field: value to write}, [(kind, what, detail)]). Labels compare on the
    managed set only, so a UI-added label is invisible here. `milestone` and
    `blocked_by` are compared only when the YAML has the key.
    """
    writes, records = {}, []
    for field in [f for f in FIELDS if f in edited]:
        new, now = edited[field], live.get(field, _ABSENT.get(field))
        o = old.get(field, _ABSENT.get(field)) if old else None
        if field == "labels":
            new, now = sorted(set(new) & managed), sorted(set(now) & managed)
            o = sorted(set(o) & managed) if old else None
        if old is None:
            if new != now:
                writes[field] = edited[field]
                records.append(("changed", "issue", _detail(title, field, now, new)))
        elif new == o:  # the agent left it alone
            if now != o:
                records.append(
                    ("skipped", "issue", f"{title}: {field} changed on GitLab, kept")
                )
        elif now != new:
            if now == o or force:
                writes[field] = edited[field]
                records.append(("changed", "issue", _detail(title, field, now, new)))
            else:
                records.append(("drift", "issue", _detail(title, field, now, new)))
    return writes, records


def index_issues(issues):
    """Live issues -> (open by stripped title, iid -> title, closed titles).

    Two open issues with one title would make the match a coin toss, so
    that is an error naming both.
    """
    opened, iids, closed = {}, {}, set()
    for issue in issues:
        title = issue.title.strip()
        if issue.state == "closed":
            closed.add(title)
            continue
        if title in opened:
            raise SpecError(
                f"two open issues titled {title!r} on GitLab "
                f"(#{opened[title].iid}, #{issue.iid}) — close or retitle one"
            )
        opened[title] = issue
        iids[issue.iid] = title
    return opened, iids, closed


def _find(spec_i, have):
    """(live title, closed?) for a spec entry: by iid when both carry one,
    else by stripped title. An open match beats a closed one."""
    iid = spec_i.get("iid")
    if iid is not None and iid in have.get("iids", {}):
        return have["iids"][iid], False
    title = spec_i["title"].strip()
    if title in have["issues"]:
        return title, False
    return None, title in have.get("closed", ())


def _old(spec_i, base_have):
    if base_have is None:
        return None
    title, _ = _find(spec_i, base_have)
    return base_have["issues"][title] if title else None


def _titles(spec, have=None):
    """{stripped title: iid or None}: what title refs in blocked_by resolve to.
    A card with no iid in the YAML that `have` already holds takes the live
    iid, so a `new:` ref stops differing once apply has created its card."""
    by_title = {t: iid for iid, t in (have or {}).get("iids", {}).items()}
    titles = {}
    for i in spec["issues"]:
        iid = i.get("iid")
        if iid is None and have:
            iid = by_title.get(_find(i, have)[0])
        titles[i["title"].strip()] = iid
    return titles


def _live_refs(title, found, want):
    """(live refs, skip rows): `found` is links.read; a footer-only ref the
    YAML omits cannot be removed here, so it is left out of the live side
    (no phantom change on every plan) and named once as skipped."""
    gone = {x["ref"] for x in found if x["source"] == "footer" and x["ref"] not in want}
    skips = [
        (
            "skipped",
            "link",
            f"{title}: blocker {links._shown(r)} is a footer ref"
            " — edit the description",
        )
        for r in sorted(gone, key=links.ref_key)
    ]
    return [x["ref"] for x in found if x["ref"] not in gone], skips


def ensure_issues(
    project,
    spec,
    record,
    users,
    base=None,
    force=False,
    milestones=None,
    project_id_of=None,
):
    """Create or update each spec issue. Returns {spec title: live issue},
    freshly created ones included; titles closed on GitLab are absent.

    Links go last, once every card exists, so a `new:` ref can resolve to
    the card this run created. Only a blocked_by the merge decided to write
    is synced, which is how skipped and drift hold for links too.
    """
    managed = managed_labels(spec, base)
    base_have = have_from_spec(base) if base else None
    opened, iids, closed = index_issues(project.issues.list(state="all", all=True))
    have = {"issues": opened, "iids": iids, "closed": closed}
    titles = _titles(spec, have)
    live, linking = {}, []
    for spec_i in spec["issues"]:
        title = spec_i["title"]
        want = wanted_issue(spec_i, spec["project"], titles)
        live_title, is_closed = _find(spec_i, have)
        if is_closed:
            record("skipped", "issue", f"{title}: closed on GitLab")
            continue
        if live_title is None:
            live[title] = project.issues.create(
                {"title": title, **_payload(want, users, milestones)}
            )
            record("added", "issue", title)
            if want.get("blocked_by"):
                linking.append((live[title], want["blocked_by"]))
            continue
        issue = live[title] = opened[live_title]
        writes = {}
        if live_title != title:
            writes["title"] = title
            record("changed", "issue", f"{live_title}: title -> {title}")
        refs = footer_skips = None
        if "blocked_by" in want:
            refs, footer_skips = _live_refs(
                title, links.read(issue, project), want["blocked_by"]
            )
        fields, records = issue_changes(
            title,
            want,
            current_issue(issue, refs),
            _old(spec_i, base_have),
            managed,
            force,
        )
        if "labels" in fields:  # keep what the UI added
            fields["labels"] = sorted(
                set(fields["labels"]) | (set(issue.labels) - managed)
            )
        if "blocked_by" in fields:
            linking.append((issue, fields["blocked_by"]))
        writes.update(_payload(fields, users, milestones))
        for change in [*records, *(footer_skips or [])]:
            record(*change)
        if writes:
            for k, v in writes.items():
                setattr(issue, k, v)
            issue.save()
    for issue, refs in linking:
        want = []
        for ref in refs:
            if ref.startswith("new:"):
                if ref[4:] not in live:  # closed on GitLab, already reported
                    record("skipped", "link", f"{issue.title}: blocker {ref} not found")
                    continue
                ref = str(live[ref[4:]].iid)
            want.append(ref)
        # footer-only refs the YAML omits were already skipped above
        have_links = [
            x
            for x in links.read(issue, project)
            if x["source"] == "native" or x["ref"] in want
        ]
        for change in links.sync(issue, project, want, have_links, project_id_of):
            record(*change)
    return live


MARKER = "*staged via gitboard*"


def marked(body):
    """A staged note as posted: the marker says a YAML wrote it, not a person."""
    return body if body.startswith(MARKER) else f"{MARKER}\n\n{body}"


def staged_notes(spec_issue):
    """`notes:` bodies to post, normalised; empty entries dropped."""
    return [b for b in map(norm_text, spec_issue.get("notes") or []) if b]


def note_line(title, body):
    return f"{title}: {body.splitlines()[0][:60]}"


def ensure_notes(project, issues, record, live=None):
    """Post each staged note whose body is not already on the issue.

    Same idempotency as migrate_comments: re-running posts nothing. Only
    issues with `notes:` cost a request, so a plain apply stays cheap.
    `live` is ensure_issues' {title: issue}; without it the board is read.
    """
    wanted = [(i["title"], staged_notes(i)) for i in issues]
    wanted = [(title, bodies) for title, bodies in wanted if bodies]
    if not wanted:
        return
    if live is None:
        live = index_issues(project.issues.list(state="all", all=True))[0]
    for title, bodies in wanted:
        issue = live.get(title.strip())
        if issue is None:  # closed on GitLab; ensure_issues already said so
            continue
        posted = {norm_text(n.body) for n in issue.notes.list(all=True)}
        for body in bodies:
            if marked(body) in posted:
                continue
            issue.notes.create({"body": marked(body)})
            record("added", "note", note_line(title, body))


def apply(gl, spec, base=None, force=False, on_change=None):
    """Write the spec. Returns the list of (kind, what, detail) changes.

    `base` is the spec as pulled: with it, a field the YAML did not touch is
    never written over a change made on GitLab, and a field both sides
    changed is reported as drift and left alone unless `force`.
    """
    changes = []

    def record(kind, what, detail):
        changes.append((kind, what, detail))
        # Debug, not info: the CLI already prints the change table, and
        # logging it again at info level says everything twice.
        log.debug("%s %s %s", kind, what, detail)
        if on_change:
            on_change(kind, what, detail)

    users = resolve_users(gl, spec)
    project = ensure_project(gl, spec["project"], spec.get("create_project", False))
    labels = ensure_labels(project, spec["columns"], record, spec.get("labels", []))
    ensure_board(project, spec["board"], spec["columns"], labels, record)
    milestones = ensure_milestones(gl, project, spec, record)
    live = ensure_issues(
        project, spec, record, users, base, force, milestones, _project_id_of(gl)
    )
    ensure_order(project, spec, base, record, force)
    ensure_notes(project, spec["issues"], record, live)
    return changes


def plan(gl, spec, base=None):
    """Read-only: what apply would do. Never writes."""
    resolve_users(gl, spec)
    try:
        project = client.get_project(gl, spec["project"])
    except client.GitlabProblem:
        return diff(spec, None, base)
    opened, iids, closed = index_issues(project.issues.list(state="all", all=True))
    labels = project.labels.list(all=True)
    have = {
        "labels": {x.name for x in labels},
        "label_meta": {
            x.name: (
                (getattr(x, "color", None) or "").lower(),
                norm_text(getattr(x, "description", None)),
            )
            for x in labels
        },
        "boards": {b.name for b in project.boards.list(all=True)},
        "issues": {title: current_issue(i) for title, i in opened.items()},
        "iids": iids,
        "closed": closed,
        "notes": {},
        "milestones": {
            title: _milestone_meta(m)
            for title, m in _live_milestones(gl, project).items()
        },
    }
    # only issues that stage notes or manage blocked_by cost a request
    titles = _titles(spec, have)
    skips = []
    for spec_i in spec["issues"]:
        title, _ = _find(spec_i, have)
        if not title:
            continue
        if staged_notes(spec_i):
            have["notes"][title] = {
                norm_text(n.body) for n in opened[title].notes.list(all=True)
            }
        if "blocked_by" in spec_i:
            want = links.norm_refs(spec_i["blocked_by"], spec["project"], titles)
            refs, skipped = _live_refs(
                spec_i["title"], links.read(opened[title], project), want
            )
            have["issues"][title] = current_issue(opened[title], refs)
            skips += skipped
    if base:  # order is managed only with a base; without one, skip the read
        have["order"] = _live_order(project)[0]
    return diff(spec, have, base) + skips


def have_from_spec(base):
    """A pulled spec as the `have` side of diff() — plan with no network.

    Both sides go through wanted_issue, so dates, trailing newlines and label
    order agree by construction.
    """
    issues = [(i["title"].strip(), i) for i in base["issues"]]
    titles = _titles(base)
    labels = [*base["columns"], *base.get("labels", [])]
    return {
        "labels": {x["name"] for x in labels},
        "label_meta": {
            x["name"]: (
                norm_color(x["color"]) if x.get("color") else "",
                norm_text(x.get("description")),
            )
            for x in labels
        },
        "boards": {base["board"]},
        "issues": {
            title: wanted_issue(i, base["project"], titles) for title, i in issues
        },
        "iids": {i["iid"]: title for title, i in issues if i.get("iid") is not None},
        "closed": set(),
        "order": [i["iid"] for i in base["issues"] if i.get("iid")],
        "milestones": {
            m["title"]: {
                "due_date": _iso(m.get("due_date")),
                "description": norm_text(m.get("description")),
            }
            for m in base.get("milestones") or []
        },
        # what the base already carries: pulled discussion plus its own
        # staged notes, so an already-staged reply is not reported twice
        "notes": {
            title: {norm_text(d["body"]) for d in i.get("discussion") or []}
            | {marked(b) for b in staged_notes(i)}
            for title, i in issues
        },
    }


def label_changes(labels, have):
    """What ensure_labels would do for the spec's `labels:` entries: create
    the missing, recolour, re-describe. Colour and description are compared
    only when the spec states them and `have` knows them."""
    pending = []
    for want in labels:
        name = want["name"]
        if name not in have["labels"]:
            pending.append(("added", "label", name))
            continue
        color, desc = have.get("label_meta", {}).get(name, ("", ""))
        if want.get("color") and color and norm_color(want["color"]) != color:
            pending.append(("changed", "label", f"{name} colour -> {want['color']}"))
        if want.get("description") is not None and norm_text(want["description"]) != (
            desc
        ):
            pending.append(("changed", "label", f"{name} description"))
    return pending


def diff(spec, have, base=None, force=False):
    """Pure: the (kind, what, detail) list apply would write. `have` is
    {"labels": set, "label_meta": {name: (hex colour, description)},
    "boards": set, "issues": {title: current_issue-shaped}, "iids": {iid:
    title}, "closed": {titles}, "notes": {title: {bodies already posted}},
    "milestones": {title: {"due_date", "description"}}, "order": [open iids
    in board order]}, or None for a
    project that does not exist yet. `base` is the spec as pulled; see
    apply()."""
    if have is None:
        pending = [("added", "project", spec["project"])]
        pending += [("added", "label", c["name"]) for c in spec["columns"]]
        pending += [("added", "label", x["name"]) for x in spec.get("labels", [])]
        pending += milestone_changes(spec, None)
        pending += [("added", "board", spec["board"])]
        pending += [("added", "issue", i["title"]) for i in spec["issues"]]
        pending += [
            ("added", "note", note_line(i["title"], b))
            for i in spec["issues"]
            for b in staged_notes(i)
        ]
        return pending

    managed = managed_labels(spec, base)
    base_have = have_from_spec(base) if base else None
    pending = [
        ("added", "label", c["name"])
        for c in spec["columns"]
        if c["name"] not in have["labels"]
    ]
    pending += label_changes(spec.get("labels", []), have)
    pending += milestone_changes(spec, have)
    if spec["board"] not in have["boards"]:
        pending.append(("added", "board", spec["board"]))
    titles = _titles(spec, have)
    for spec_i in spec["issues"]:
        title = spec_i["title"]
        live_title, is_closed = _find(spec_i, have)
        if is_closed:
            pending.append(("skipped", "issue", f"{title}: closed on GitLab"))
            continue
        if live_title is None:
            pending.append(("added", "issue", title))
            continue
        if live_title != title:
            pending.append(("changed", "issue", f"{live_title}: title -> {title}"))
        _, records = issue_changes(
            title,
            wanted_issue(spec_i, spec["project"], titles),
            have["issues"][live_title],
            _old(spec_i, base_have),
            managed,
            force,
        )
        pending += records
    if "order" in have:  # offline, have_from_spec(.base) is the old order too
        old = base or {"issues": [{"iid": i} for i in have["order"]]}
        pending += order_changes(spec, have["order"], old, force)[0]
    posted = have.get("notes", {})
    for spec_i in spec["issues"]:
        live_title, is_closed = _find(spec_i, have)
        if is_closed:
            continue
        for body in staged_notes(spec_i):
            if marked(body) not in posted.get(live_title, set()):
                pending.append(("added", "note", note_line(spec_i["title"], body)))
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
