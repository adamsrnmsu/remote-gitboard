"""Tests for migrate.py. No network — the API surface is faked.

The contract under test: every op plans as pending, applies, and plans as
skipped afterwards; one-way ops leave exactly one note per card and none on
a rerun; plan never writes.
"""

import types

import pytest

from gitboard import client, migrate
from gitboard.apply import SpecError


class FakeNotes:
    def __init__(self, bodies=()):
        self.notes = [types.SimpleNamespace(body=b) for b in bodies]

    def list(self, **_):
        return list(self.notes)

    def create(self, payload):
        self.notes.append(types.SimpleNamespace(body=payload["body"]))


class FakeIssue:
    def __init__(self, iid, labels=(), state="opened", notes=()):
        self.iid, self.labels, self.state = iid, sorted(labels), state
        self.notes, self.saved, self.project = FakeNotes(notes), 0, None

    def save(self):
        self.saved += 1

    def move(self, to_id):
        """GitLab closes the source with a "moved to" note and creates a copy
        on the target; python-gitlab mutates this object into the copy."""
        src, target = self.project.issues.issues, PROJECTS[to_id]
        twin = FakeIssue(self.iid, self.labels, "closed", [f"moved to {to_id}"])
        src[src.index(self)] = twin
        target.issues.append(self)
        self.iid, self.project = len(target.issues.issues) + 500, target


class FakeLabel:
    def __init__(self, manager, name, color="#ff0000"):
        self.manager, self.name, self.color, self.new_name = manager, name, color, None
        self.id = name  # ensure_board hands label.id to lists.create

    def save(self):
        if self.new_name:
            self.name, self.new_name = self.new_name, None

    def delete(self):
        self.manager.labels.remove(self)


class FakeLabels:
    def __init__(self, names):
        self.labels = [FakeLabel(self, n) for n in names]

    def list(self, **_):
        return list(self.labels)

    def create(self, payload):
        label = FakeLabel(self, payload["name"], payload["color"])
        self.labels.append(label)
        return label


class FakeList:
    def __init__(self, board, name, position):
        self.board, self.label, self.position = board, {"name": name}, position
        self.id, self.saved = position + 1, 0

    def save(self):
        """Like GitLab: a move re-numbers every other list, and a move to the
        position the list already holds is refused with a 400."""
        others = sorted(
            (x for x in self.board.lists_ if x is not self), key=lambda x: x.position
        )
        current = [x.position for x in others]  # gap-free apart from ours
        was = next((i for i in range(len(others) + 1) if i not in current), None)
        if was == self.position:
            raise RuntimeError("400: List could not be moved!")
        order = others[: self.position] + [self] + others[self.position :]
        for i, x in enumerate(order):
            x.position = i
        self.saved += 1

    def delete(self):
        self.board.lists_.remove(self)


class FakeBoard:
    def __init__(self, id_, name, columns):
        self.id, self.name = id_, name
        self.lists_ = [FakeList(self, n, i) for i, n in enumerate(columns)]
        self.lists = types.SimpleNamespace(
            list=lambda **_: list(self.lists_), create=self._add
        )

    def _add(self, payload):
        self.lists_.append(FakeList(self, payload["label_id"], len(self.lists_)))


class FakeIssues:
    def __init__(self, issues):
        self.issues = list(issues)

    def append(self, issue):
        self.issues.append(issue)

    def list(self, labels=(), state="opened", **_):
        return [
            i
            for i in self.issues
            if set(labels) <= set(i.labels) and (state == "all" or i.state == state)
        ]

    def get(self, iid):
        return next(i for i in self.issues if i.iid == iid)


PROJECTS = {}


class FakeProject:
    def __init__(self, id_, path, labels=(), boards=None, issues=()):
        self.id, self.path_with_namespace = id_, path
        self.labels, self.issues = FakeLabels(labels), FakeIssues(issues)
        for issue in self.issues.issues:
            issue.project = self
        self.boards_ = [
            FakeBoard(i + 1, n, cols)
            for i, (n, cols) in enumerate((boards or {}).items())
        ]
        self.boards = types.SimpleNamespace(
            list=lambda **_: list(self.boards_),
            get=lambda id_: next(b for b in self.boards_ if b.id == id_),
            create=self._board,
        )
        PROJECTS[id_] = self

    def _board(self, payload):
        board = FakeBoard(len(self.boards_) + 1, payload["name"], [])
        self.boards_.append(board)
        return board

    def board(self, name="Dev Board"):
        return next(b for b in self.boards_ if b.name == name)


@pytest.fixture
def gl(monkeypatch):
    PROJECTS.clear()
    monkeypatch.setattr(
        client,
        "get_project",
        lambda _gl, path: next(
            p for p in PROJECTS.values() if p.path_with_namespace == path
        ),
    )
    return types.SimpleNamespace()


def project(
    labels=("bug", "Doing", "Verify", "Done"),
    columns=("Doing", "Verify", "Done"),
    issues=(),
):
    return FakeProject(1, "grp/proj", labels, {"Dev Board": list(columns)}, issues)


def mig(*ops):
    return {"project": "grp/proj", "board": "Dev Board", "ops": list(ops)}


def run(gl, *ops):
    """plan -> apply -> plan again; returns (pending, applied, after)."""
    m = mig(*ops)
    pending = migrate.plan(gl, m)
    applied = migrate.apply(gl, m, lambda *_: None)
    return pending, applied, migrate.plan(gl, m)


# --- load --------------------------------------------------------------------


def write(tmp_path, text):
    path = tmp_path / "m.migration.yaml"
    path.write_text(text)
    return path


HEAD = "project: grp/proj\nboard: Dev Board\nops:\n"


@pytest.mark.parametrize(
    "op",
    [
        "rename_label: {from: Verify, to: QA}",
        "rename_label: {from: QA, to: Done}",
        "merge_labels: {from: [Failed, x], to: y}",
        "drop_column: {name: Done}",
    ],
)
def test_load_refuses_the_fixed_names(tmp_path, op):
    with pytest.raises(SpecError, match="is a fixed column"):
        migrate.load(write(tmp_path, HEAD + f"  - {op}\n"))


def test_load_rejects_malformed_ops(tmp_path):
    with pytest.raises(SpecError, match="unknown op 'explode'"):
        migrate.load(write(tmp_path, HEAD + "  - explode: {name: x}\n"))
    with pytest.raises(SpecError, match="needs 'to'"):
        migrate.load(write(tmp_path, HEAD + "  - rename_label: {from: a}\n"))
    with pytest.raises(SpecError, match="'ops' must be a list"):
        migrate.load(write(tmp_path, "project: p\nboard: b\nops: {a: 1}\n"))
    with pytest.raises(SpecError, match="exactly one of"):
        migrate.load(write(tmp_path, HEAD + "  - move_issues: {to: a/b}\n"))


def test_load_normalises_every_op(tmp_path):
    text = HEAD + (
        "  - rename_label: {from: bug, to: type::bug}\n"
        "  - order_columns: [Doing, Done]\n"
        "  - merge_labels: {from: [p1], to: prio}\n"
        "  - drop_column: {name: Blocked}\n"
        "  - move_issues: {iids: [1, 2], to: grp/other}\n"
        "  - split_board: {name: Other, columns: [Doing]}\n"
    )
    ops = migrate.load(write(tmp_path, text))["ops"]
    assert [next(iter(o)) for o in ops] == list(migrate.OPS)
    assert ops[3] == {"drop_column": {"name": "Blocked", "keep_label": True}}


# --- rename_label + order_columns --------------------------------------------


def test_rename_label_pending_applied_then_skipped(gl):
    p = project()
    pending, applied, after = run(
        gl, {"rename_label": {"from": "bug", "to": "type::bug"}}
    )
    assert pending == applied == [("changed", "rename_label", "bug -> type::bug")]
    assert after == [("skipped", "rename_label", "bug -> type::bug: already renamed")]
    assert {x.name for x in p.labels.list()} == {"type::bug", "Doing", "Verify", "Done"}


def test_order_columns_keeps_unnamed_lists_after_the_named(gl):
    p = project(columns=("Done", "Verify", "Doing", "Blocked"))
    pending, applied, after = run(gl, {"order_columns": ["Doing", "Verify"]})
    assert (
        pending
        == applied
        == [("changed", "order_columns", "Doing, Verify, Done, Blocked")]
    )
    assert after[0][0] == "skipped"
    lists = sorted(p.board().lists_, key=lambda x: x.position)
    assert [x.label["name"] for x in lists] == ["Doing", "Verify", "Done", "Blocked"]


def test_plan_rejects_bad_rename_and_unknown_column(gl):
    project(labels=("bug", "type::bug"))
    with pytest.raises(SpecError, match="use merge_labels"):  # both labels exist
        migrate.plan(gl, mig({"rename_label": {"from": "bug", "to": "type::bug"}}))
    with pytest.raises(SpecError, match="no such column Nope"):
        migrate.plan(gl, mig({"order_columns": ["Nope"]}))


# --- merge_labels + drop_column ----------------------------------------------


def test_merge_posts_one_note_per_card_and_none_on_rerun(gl):
    a, b = FakeIssue(1, ["p1", "Doing"]), FakeIssue(2, ["urgent", "p1"], state="closed")
    c = FakeIssue(3, ["Doing"])
    p = project(labels=("p1", "urgent", "Doing"), issues=[a, b, c])
    op = {"merge_labels": {"from": ["p1", "urgent"], "to": "priority::high"}}
    pending, applied, after = run(gl, op)
    assert (
        pending
        == applied
        == [("oneway", "merge_labels", "p1, urgent -> priority::high (2 cards)")]
    )
    assert after == [
        ("skipped", "merge_labels", "p1, urgent -> priority::high: labels already gone")
    ]
    assert a.labels == ["Doing", "priority::high"] and b.labels == ["priority::high"]
    note = "*migrated by gitboard: p1, urgent -> priority::high*"
    assert [n.body for n in a.notes.list()] == [note]
    assert [n.body for n in b.notes.list()] == [note]
    assert c.notes.list() == [] and c.saved == 0
    have = {x.name: x.color for x in p.labels.list()}
    assert "p1" not in have and "urgent" not in have
    assert have["priority::high"] == "#ff0000"  # first `from` label's colour


def test_merge_does_not_repost_a_note_a_card_already_carries(gl):
    a = FakeIssue(1, ["p1"], notes=["*migrated by gitboard: p1 -> prio*"])
    project(labels=("p1",), issues=[a])
    migrate.apply(
        gl, mig({"merge_labels": {"from": ["p1"], "to": "prio"}}), lambda *_: None
    )
    assert len(a.notes.list()) == 1


def test_drop_column_keeps_the_label_by_default(gl):
    a = FakeIssue(1, ["Verify"])
    p = project(issues=[a])
    pending, applied, after = run(gl, {"drop_column": {"name": "Doing"}})
    assert pending == applied == [("oneway", "drop_column", "Doing (0 cards)")]
    assert after == [("skipped", "drop_column", "Doing: list already gone")]
    assert [x.label["name"] for x in p.board().lists_] == ["Verify", "Done"]
    assert "Doing" in {x.name for x in p.labels.list()}


def test_drop_column_without_keep_label_strips_and_notes(gl):
    a, b = FakeIssue(1, ["Doing", "bug"]), FakeIssue(2, ["Done"])
    p = project(issues=[a, b])
    pending, applied, after = run(
        gl, {"drop_column": {"name": "Doing", "keep_label": False}}
    )
    assert pending == applied == [("oneway", "drop_column", "Doing (1 cards)")]
    assert after == [("skipped", "drop_column", "Doing: list and label already gone")]
    assert a.labels == ["bug"] and b.saved == 0
    assert [n.body for n in a.notes.list()] == ["*migrated by gitboard: dropped Doing*"]
    assert "Doing" not in {x.name for x in p.labels.list()}


# --- move_issues + split_board -----------------------------------------------


def test_move_creates_missing_target_labels_then_moves(gl):
    a, b = FakeIssue(1, ["epic::Billing", "Doing"]), FakeIssue(2, ["bug"])
    project(labels=("epic::Billing", "Doing", "bug"), issues=[a, b])
    target = FakeProject(2, "grp/billing", labels=("Doing",))
    rows = []
    op = {"move_issues": {"label": "epic::Billing", "to": "grp/billing"}}
    pending = migrate.plan(gl, mig(op))
    assert pending == [
        ("oneway", "move_issues", "epic::Billing -> grp/billing (1 cards)")
    ]
    assert {x.name for x in target.labels.list()} == {"Doing"}  # plan wrote nothing
    applied = migrate.apply(gl, mig(op), lambda *r: rows.append(r))
    assert (
        applied
        == rows
        == [
            ("added", "label", "grp/billing: epic::Billing (#ff0000)"),
            ("oneway", "move_issues", "#1 -> grp/billing#501"),
        ]
    )
    assert a.project is target and a.iid == 501
    assert migrate.plan(gl, mig(op)) == [
        ("skipped", "move_issues", "epic::Billing -> grp/billing: already moved")
    ]


def test_move_by_iids(gl):
    a, b = FakeIssue(1, ["bug"]), FakeIssue(2, ["bug"])
    project(labels=("bug",), issues=[a, b])
    FakeProject(2, "grp/other", labels=("bug",))
    pending, applied, after = run(gl, {"move_issues": {"iids": [2], "to": "grp/other"}})
    assert pending == [("oneway", "move_issues", "#2 -> grp/other (1 cards)")]
    assert applied == [("oneway", "move_issues", "#2 -> grp/other#501")]
    assert after == [("skipped", "move_issues", "#2 -> grp/other: already moved")]
    assert a.state == "opened"


def test_split_board_creates_labels_and_board_then_skips(gl):
    p = project(labels=("Doing",))
    rows = []
    op = {"split_board": {"name": "Billing board", "columns": ["Doing", "QA"]}}
    assert migrate.plan(gl, mig(op)) == [
        ("changed", "split_board", "Billing board: Doing, QA")
    ]
    assert len(p.boards_) == 1
    migrate.apply(gl, mig(op), lambda *r: rows.append(r))
    assert ("added", "board", "Billing board") in rows
    assert ("added", "label", "QA (#428bca)") in rows
    assert ("changed", "split_board", "Billing board: Doing, QA") in rows
    assert migrate.plan(gl, mig(op)) == [
        ("skipped", "split_board", "Billing board: Doing, QA: board already has them")
    ]


# --- the run ---------------------------------------------------------------


def test_apply_runs_ops_in_order_and_stops_at_the_first_failure(gl):
    p = project()
    ops = [
        {"rename_label": {"from": "bug", "to": "type::bug"}},
        {"order_columns": ["Nope"]},
        {"drop_column": {"name": "Doing"}},
    ]
    with pytest.raises(SpecError):
        migrate.apply(gl, mig(*ops), lambda *_: None)
    assert "type::bug" in {x.name for x in p.labels.list()}  # first op landed
    assert len(p.board().lists_) == 3  # third never ran


def test_touched_sums_the_oneway_counts():
    rows = [
        ("changed", "rename_label", "a -> b"),
        ("oneway", "merge_labels", "a, b -> c (3 cards)"),
        ("oneway", "drop_column", "d (12 cards)"),
        ("skipped", "move_issues", "x -> y: already moved"),
    ]
    assert migrate.touched(rows) == 15
