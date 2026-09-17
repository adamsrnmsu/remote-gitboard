"""Board migrations: the one-way edits `apply` refuses to make.

`apply` is additive so the agent can run it: it never renames, merges or
deletes labels, never removes or reorders board lists, never moves a card
to another project. A reformat needs exactly those, so they live here, in
a declarative `*.migration.yaml` the agent may draft and only a person runs
(`gitboard migrate FILE`). `plan` reads and reports; `apply` writes op by
op in file order and stops at the first failure — every op is rerunnable,
so the fix is to rerun.

Fixed names: `Verify`, `Done`, `Failed` may not be renamed, merged or
dropped. Verdicts, stats and the 8-week trend read them by name; a rename
would silently change what the numbers mean.

Idempotency: reversible ops compare state (`to` exists and `from` does not;
the lists already sit in that order; the board already has those columns).
One-way ops leave one note per touched card, `NOTE`, and that note is the
marker — a card that carries it is not touched again, and a label that is
already gone counts as merged or dropped.
"""

import yaml

from gitboard import apply as board_yaml
from gitboard import client
from gitboard.apply import SpecError, norm_text

FIXED = {"Verify", "Done", "Failed"}
NOTE = "*migrated by gitboard: {}*"


def _fixed(name):
    if name in FIXED:
        raise SpecError(
            f"{name} is a fixed column: stats and verdicts depend on it; "
            "migrate the other labels around it"
        )
    return name


def _str(op, args, key):
    value = args.get(key) if isinstance(args, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise SpecError(f"{op}: needs {key!r} (a name)")
    return value.strip()


def _names(op, value, key):
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise SpecError(f"{op}: {key!r} must be a list of names")
    if not value:
        raise SpecError(f"{op}: {key!r} is empty")
    return [x.strip() for x in value]


def _check(name, args):
    """One op's args, normalised, or SpecError."""
    if name == "rename_label":
        return {
            "from": _fixed(_str(name, args, "from")),
            "to": _fixed(_str(name, args, "to")),
        }
    if name == "order_columns":
        return _names(name, args, "columns")
    if name == "merge_labels":
        if not isinstance(args, dict):
            raise SpecError(f"{name}: needs 'from' (a list) and 'to'")
        return {
            "from": [_fixed(x) for x in _names(name, args.get("from"), "from")],
            "to": _fixed(_str(name, args, "to")),
        }
    if name == "drop_column":
        keep = args.get("keep_label", True) if isinstance(args, dict) else True
        if not isinstance(keep, bool):
            raise SpecError(f"{name}: keep_label must be true or false")
        return {"name": _fixed(_str(name, args, "name")), "keep_label": keep}
    if name == "move_issues":
        to = _str(name, args, "to")
        has = [k for k in ("label", "iids") if args.get(k) is not None]
        if len(has) != 1:
            raise SpecError(f"{name}: give exactly one of 'label' or 'iids'")
        if has == ["label"]:
            return {"label": _str(name, args, "label"), "to": to}
        iids = args["iids"]
        if not isinstance(iids, list) or not all(isinstance(i, int) for i in iids):
            raise SpecError(f"{name}: 'iids' must be a list of issue numbers")
        return {"iids": iids, "to": to}
    if name == "split_board":
        if not isinstance(args, dict):
            raise SpecError(f"{name}: needs 'name' and 'columns'")
        return {
            "name": _str(name, args, "name"),
            "columns": _names(name, args.get("columns"), "columns"),
        }
    raise SpecError(f"unknown op {name!r} — one of: {', '.join(sorted(OPS))}")


def load(path):
    try:
        with open(path) as f:
            mig = yaml.safe_load(f)
    except FileNotFoundError as e:
        raise SpecError(f"no such migration file: {path}") from e
    except OSError as e:
        raise SpecError(f"cannot read {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise SpecError(f"{path}: {e}") from e
    if not isinstance(mig, dict):
        raise SpecError(f"{path}: expected a mapping at the top level")
    for key in ("project", "board"):
        if not isinstance(mig.get(key), str):
            raise SpecError(f"{path}: missing required key {key!r}")
    ops = mig.get("ops")
    if not isinstance(ops, list):
        raise SpecError(f"{path}: 'ops' must be a list")
    checked = []
    for op in ops:
        if not isinstance(op, dict) or len(op) != 1:
            raise SpecError(
                f"{path}: each op is one mapping like `rename_label: {{...}}`"
            )
        (name, args), *_ = op.items()
        try:
            checked.append({name: _check(name, args)})
        except SpecError as e:
            raise SpecError(f"{path}: {e}") from e
    return {"project": mig["project"], "board": mig["board"], "ops": checked}


# --- the ops -----------------------------------------------------------------
# Each takes (ctx, args, do) and returns [(kind, what, detail)]. `do=False`
# is plan: same reads, same rows, no writes. That is what keeps plan and
# apply from drifting apart.


class _Ctx:
    def __init__(self, gl, project, board_name, record):
        self.gl, self.project, self.board_name, self.record = (
            gl,
            project,
            board_name,
            record,
        )
        self._board = None

    def labels(self):
        return {x.name: x for x in self.project.labels.list(all=True)}

    def board(self):
        if self._board is None:
            found = next(
                (
                    b
                    for b in self.project.boards.list(all=True)
                    if b.name == self.board_name
                ),
                None,
            )
            if found is None:
                path = self.project.path_with_namespace
                raise SpecError(f"no board {self.board_name!r} in {path}")
            self._board = self.project.boards.get(found.id)
        return self._board

    def lists(self):
        """Label lists in position order; assignee/milestone lists are skipped."""
        lists = [
            x for x in self.board().lists.list(all=True) if getattr(x, "label", None)
        ]
        return sorted(lists, key=lambda x: x.position)

    def carrying(self, label):
        return self.project.issues.list(labels=[label], state="all", all=True)


def _noted(issue, note):
    return any(norm_text(n.body) == note for n in issue.notes.list(all=True))


def _relabel(issue, drop, add, note):
    issue.labels = sorted((set(issue.labels) - drop) | add)
    issue.save()
    if not _noted(issue, note):
        issue.notes.create({"body": note})


def rename_label(ctx, args, do):
    src, dst, have = args["from"], args["to"], ctx.labels()
    if src in have and dst in have:
        raise SpecError(f"both {src!r} and {dst!r} exist — use merge_labels")
    if src not in have:
        if dst not in have:
            raise SpecError(f"no label {src!r} to rename")
        return [("skipped", "rename_label", f"{src} -> {dst}: already renamed")]
    if do:
        have[src].new_name = dst
        have[src].save()
    return [("changed", "rename_label", f"{src} -> {dst}")]


def order_columns(ctx, args, do):
    current = ctx.lists()
    by_name = {x.label["name"]: x for x in current}
    unknown = [n for n in args if n not in by_name]
    if unknown:
        raise SpecError(f"order_columns: no such column {', '.join(unknown)}")
    named = set(args)
    wanted = [by_name[n] for n in args] + [
        x for x in current if x.label["name"] not in named
    ]
    detail = ", ".join(x.label["name"] for x in wanted)
    if wanted == current:
        return [("skipped", "order_columns", f"{detail}: already in that order")]
    if do:
        # GitLab shifts the other lists on every move and answers 400 to a
        # move that changes nothing, so re-read after each save and only
        # move a list that is not already where it belongs.
        for i, target in enumerate(wanted):
            live = {x.id: x for x in ctx.lists()}
            if live[target.id].position != i:
                live[target.id].position = i
                live[target.id].save()
    return [("changed", "order_columns", detail)]


def merge_labels(ctx, args, do):
    src, dst, have = args["from"], args["to"], ctx.labels()
    present = [f for f in src if f in have]
    what = f"{', '.join(src)} -> {dst}"
    if not present:
        return [("skipped", "merge_labels", f"{what}: labels already gone")]
    cards = {i.iid: i for f in present for i in ctx.carrying(f)}
    drop = set(src)
    pending = [i for i in cards.values() if set(i.labels) & drop]
    if do:
        if dst not in have:
            ctx.project.labels.create({"name": dst, "color": have[present[0]].color})
        for issue in pending:
            _relabel(issue, drop, {dst}, NOTE.format(what))
        for f in present:
            have[f].delete()
    return [("oneway", "merge_labels", f"{what} ({len(pending)} cards)")]


def drop_column(ctx, args, do):
    name, keep = args["name"], args.get("keep_label", True)
    lst = next((x for x in ctx.lists() if x.label["name"] == name), None)
    label = None if keep else ctx.labels().get(name)
    cards = ctx.carrying(name) if label else []
    if lst is None and label is None:
        why = "list already gone" if keep else "list and label already gone"
        return [("skipped", "drop_column", f"{name}: {why}")]
    if do:
        if lst is not None:
            lst.delete()
        if label is not None:
            for issue in cards:
                _relabel(issue, {name}, set(), NOTE.format(f"dropped {name}"))
            label.delete()
    return [("oneway", "drop_column", f"{name} ({len(cards)} cards)")]


def _moved(issue):
    return issue.state == "closed" and any(
        "moved to" in n.body for n in issue.notes.list(all=True)
    )


def move_issues(ctx, args, do):
    to = args["to"]
    if "label" in args:
        cards, src = ctx.carrying(args["label"]), args["label"]
    else:
        cards = [client.get_issue(ctx.project, i) for i in args["iids"]]
        src = ", ".join(f"#{i}" for i in args["iids"])
    moving = [i for i in cards if not _moved(i)]
    if not moving:
        return [("skipped", "move_issues", f"{src} -> {to}: already moved")]
    if not do:
        return [("oneway", "move_issues", f"{src} -> {to} ({len(moving)} cards)")]
    target = client.get_project(ctx.gl, to)
    # labels carry over only if the target already has them by name
    have, ours = {x.name for x in target.labels.list(all=True)}, ctx.labels()
    for name in sorted({x for i in moving for x in i.labels} - have):
        color = ours[name].color if name in ours else "#428bca"
        target.labels.create({"name": name, "color": color})
        ctx.record("added", "label", f"{to}: {name} ({color})")
    rows = []
    for issue in moving:
        src_iid = issue.iid
        issue.move(target.id)
        rows.append(("oneway", "move_issues", f"#{src_iid} -> {to}#{issue.iid}"))
    return rows


def split_board(ctx, args, do):
    name, columns = args["name"], args["columns"]
    cols = [{"name": c} for c in columns]
    detail = f"{name}: {', '.join(columns)}"
    board = next((b for b in ctx.project.boards.list(all=True) if b.name == name), None)
    if board is not None:
        listed = {
            x.label["name"]
            for x in ctx.project.boards.get(board.id).lists.list(all=True)
            if getattr(x, "label", None)
        }
        if set(columns) <= listed:
            return [("skipped", "split_board", f"{detail}: board already has them")]
    if do:
        labels = board_yaml.ensure_labels(ctx.project, cols, ctx.record)
        board_yaml.ensure_board(ctx.project, name, cols, labels, ctx.record)
    return [("changed", "split_board", detail)]


OPS = {
    "rename_label": rename_label,
    "order_columns": order_columns,
    "merge_labels": merge_labels,
    "drop_column": drop_column,
    "move_issues": move_issues,
    "split_board": split_board,
}


def _run(gl, mig, do, on_row=None):
    rows = []

    def record(kind, what, detail):
        rows.append((kind, what, detail))
        if on_row:
            on_row(kind, what, detail)

    with client.write_errors():
        ctx = _Ctx(gl, client.get_project(gl, mig["project"]), mig["board"], record)
        for op in mig["ops"]:
            (name, args), *_ = op.items()
            for row in OPS[name](ctx, args, do):
                record(*row)
    return rows


def plan(gl, mig):
    """Read-only: what apply would do. Never writes."""
    return _run(gl, mig, False)


def apply(gl, mig, record):
    """Apply pending ops in file order; stops at the first failure."""
    return _run(gl, mig, True, record)


def touched(pending):
    """Cards the one-way rows would touch — the number to look at first."""
    total = 0
    for kind, _, detail in pending:
        if kind == "oneway" and detail.endswith(" cards)"):
            total += int(detail.rsplit("(", 1)[1].split()[0])
    return total
