"""Tests for apply.py. No network — the GitLab API surface is faked.

The two bugs these pin both cost a real debugging round: YAML parses an
unquoted date into a date object (not JSON-serialisable), and GitLab strips
the trailing newline that YAML's `|` preserves (phantom diff on every run).
"""

import datetime
import types

import pytest
from gitlab.exceptions import GitlabGetError

from gitboard import apply, client


class FakeIssue:
    def __init__(
        self,
        title,
        labels=(),
        description="",
        due_date=None,
        assignee=None,
        iid=1,
        state="opened",
        notes=(),
        milestone=None,
        links=(),
    ):
        self.title, self.labels, self.iid = title, list(labels), iid
        self.id, self.relative_position = iid + 1000, None
        self.description, self.due_date = description, due_date
        self.assignee = {"username": assignee} if assignee else None
        self.milestone = milestone  # the API's dict: title, due_date, description
        self.state, self.saved = state, False
        self.notes = FakeNotes([fake_note(b) for b in notes])
        self.links = FakeLinks(links)

    def save(self):
        self.saved = True


class FakeProject:
    def __init__(self, labels=(), boards=(), issues=(), milestones=()):
        self.id, self.path_with_namespace = 1, "grp/proj"
        self.namespace = {"kind": "user", "full_path": "grp"}
        self.labels = _lister([types.SimpleNamespace(name=n) for n in labels])
        self.boards = _lister([types.SimpleNamespace(name=n) for n in boards])
        self.issues = _lister(list(issues))
        self.issues.create = self._create
        self.milestones = _lister(list(milestones))
        self.milestones.create = self._create_milestone

    def _create_milestone(self, payload):
        items = self.milestones.list()
        found = milestone(id=len(items) + 50, **payload)
        found.created = payload
        items.append(found)
        return found

    def _create(self, payload):
        items = self.issues.list()
        issue = FakeIssue(payload["title"], iid=len(items) + 100)
        for k, v in payload.items():
            setattr(issue, k, v)
        items.append(issue)
        return issue


def _lister(items):
    return types.SimpleNamespace(list=lambda **_: items)


def milestone(title, due_date=None, description="", id=50):
    m = types.SimpleNamespace(
        title=title, due_date=due_date, description=description, id=id, saved=False
    )
    m.save = lambda: setattr(m, "saved", True)
    return m


def link(iid, project_id=1, kind="is_blocked_by", link_id=1, full=None):
    return types.SimpleNamespace(
        iid=iid,
        project_id=project_id,
        link_type=kind,
        issue_link_id=link_id,
        link_created_at="2026-09-20T10:00:00Z",
        references={"full": full or f"grp/proj#{iid}"},
    )


class FakeLinks:
    """issue.links; `downgrade` answers the way CE does, storing relates_to."""

    def __init__(self, items=(), downgrade=False):
        self.items, self.downgrade = list(items), downgrade
        self.created, self.deleted, self.listed = [], [], 0

    def list(self, **_):
        self.listed += 1
        return list(self.items)

    def create(self, data):
        self.created.append(data)
        kind = "relates_to" if self.downgrade else data["link_type"]
        self.items.append(
            link(
                data["target_issue_iid"],
                data["target_project_id"],
                kind,
                900 + len(self.created),
            )
        )

    def delete(self, link_id):
        self.deleted.append(link_id)
        self.items = [x for x in self.items if x.issue_link_id != link_id]


def use_project(monkeypatch, project):
    """Patch client.get_project directly — its error mapping has its own tests.
    None means 'no such project', which plan() treats as a fresh build."""

    def get_project(_gl, path):
        if project is None:
            raise client.GitlabProblem(f"no project {path!r}")
        return project

    monkeypatch.setattr(apply.client, "get_project", get_project)
    return types.SimpleNamespace()


SPEC = {
    "project": "grp/proj",
    "board": "Dev Board",
    "columns": [{"name": "Doing"}, {"name": "Blocked"}],
    "issues": [{"title": "one", "labels": ["Doing"]}],
}


# --- normalisation ---------------------------------------------------------


def test_norm_text_strips_the_trailing_newline_yaml_block_scalars_add():
    assert apply.norm_text("body\n") == "body"


def test_norm_text_normalises_crlf():
    assert apply.norm_text("a\r\nb") == "a\nb"


def test_norm_text_handles_none():
    assert apply.norm_text(None) == ""


def test_yaml_date_becomes_an_iso_string():
    """PyYAML gives a date object; requests cannot JSON-encode it."""
    want = apply.wanted_issue({"title": "t", "due_date": datetime.date(2026, 9, 1)})
    assert want["due_date"] == "2026-09-01"


def test_quoted_date_passes_through_unchanged():
    want = apply.wanted_issue({"title": "t", "due_date": "2026-09-01"})
    assert want["due_date"] == "2026-09-01"


def test_labels_are_sorted_so_yaml_order_is_not_a_diff():
    a = apply.wanted_issue({"title": "t", "labels": ["b", "a"]})
    b = apply.wanted_issue({"title": "t", "labels": ["a", "b"]})
    assert a["labels"] == b["labels"]


def test_a_settled_issue_shows_no_drift():
    """wanted_issue and current_issue must agree, or apply is never idempotent."""
    spec = {
        "title": "t",
        "labels": ["Doing"],
        "description": "body\n",
        "milestone": None,
    }
    issue = FakeIssue("t", labels=["Doing"], description="body")
    assert apply.wanted_issue(spec) == apply.current_issue(issue)


# --- plan ------------------------------------------------------------------


def test_plan_on_a_missing_project_lists_everything(monkeypatch):
    gl = use_project(monkeypatch, None)
    pending = apply.plan(gl, SPEC)
    assert pending[0] == ("added", "project", "grp/proj")
    assert ("added", "board", "Dev Board") in pending
    assert len(pending) == 5  # project, 2 labels, board, 1 issue


def test_plan_reports_nothing_when_the_board_already_matches(monkeypatch):
    project = FakeProject(
        labels=["Doing", "Blocked"],
        boards=["Dev Board"],
        issues=[FakeIssue("one", labels=["Doing"])],
    )
    assert apply.plan(use_project(monkeypatch, project), SPEC) == []


def test_plan_spots_a_changed_label(monkeypatch):
    project = FakeProject(
        labels=["Doing", "Blocked"],
        boards=["Dev Board"],
        issues=[FakeIssue("one", labels=["Blocked"])],
    )
    pending = apply.plan(use_project(monkeypatch, project), SPEC)
    assert pending == [("changed", "issue", "one: labels [Blocked] -> [Doing]")]


def test_plan_never_proposes_deleting_what_the_yaml_omits(monkeypatch):
    """Additive only — an issue not in the YAML is left alone, not removed."""
    project = FakeProject(
        labels=["Doing", "Blocked"],
        boards=["Dev Board"],
        issues=[FakeIssue("one", labels=["Doing"]), FakeIssue("not in yaml")],
    )
    assert apply.plan(use_project(monkeypatch, project), SPEC) == []


# --- load ------------------------------------------------------------------


def test_load_rejects_a_spec_with_no_board(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text("project: grp/proj\n")
    with pytest.raises(apply.SpecError) as e:
        apply.load(str(f))
    assert "board" in str(e.value)


def test_load_defaults_columns_and_issues_to_empty(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text("project: grp/proj\nboard: B\n")
    spec = apply.load(str(f))
    assert spec["columns"] == [] and spec["issues"] == []


# --- migrate_comments ------------------------------------------------------


def fake_note(body, author="alice", created="2026-08-01T10:00:00Z", system=False):
    return types.SimpleNamespace(
        body=body, system=system, author={"username": author}, created_at=created
    )


class FakeNotes:
    def __init__(self, notes=()):
        self.notes = list(notes)

    def list(self, **_):
        return list(self.notes)

    def create(self, payload):
        self.notes.append(fake_note(payload["body"], author="token-owner"))


def migration_project(monkeypatch, src_notes, dst_notes=()):
    issues = {
        1: types.SimpleNamespace(iid=1, notes=FakeNotes(src_notes)),
        2: types.SimpleNamespace(iid=2, notes=FakeNotes(dst_notes)),
    }
    project = types.SimpleNamespace(
        path_with_namespace="grp/proj",
        issues=types.SimpleNamespace(get=lambda iid: issues[iid]),
    )
    use_project(monkeypatch, project)
    return issues


def test_comments_are_copied_oldest_first_with_attribution(monkeypatch):
    issues = migration_project(
        monkeypatch,
        [
            fake_note("second", author="bob", created="2026-08-02T00:00:00Z"),
            fake_note("first", author="alice", created="2026-08-01T00:00:00Z"),
        ],
    )
    assert apply.migrate_comments(None, "grp/proj", 1, 2) == 2
    bodies = [n.body for n in issues[2].notes.notes]
    assert bodies[0] == "*from #1, by @alice on 2026-08-01:*\n\nfirst"
    assert bodies[1].startswith("*from #1, by @bob on 2026-08-02:*")


def test_system_notes_are_not_comments(monkeypatch):
    issues = migration_project(
        monkeypatch,
        [fake_note("added label ~Doing", system=True), fake_note("real comment")],
    )
    assert apply.migrate_comments(None, "grp/proj", 1, 2) == 1
    assert len(issues[2].notes.notes) == 1


def test_migration_is_idempotent(monkeypatch):
    issues = migration_project(monkeypatch, [fake_note("hello")])
    assert apply.migrate_comments(None, "grp/proj", 1, 2) == 1
    assert apply.migrate_comments(None, "grp/proj", 1, 2) == 0
    assert len(issues[2].notes.notes) == 1


# --- spec_from_board -------------------------------------------------------


def board_fixture():
    issue = FakeIssue(
        "one", labels=["Doing"], description="body\n", due_date="2026-09-01", iid=7
    )
    project = FakeProject(labels=["Doing"], boards=["Dev Board"], issues=[issue])
    project.labels.list()[0].color = "#428bca"
    project.path_with_namespace = "grp/proj"
    board = types.SimpleNamespace(name="Dev Board")
    columns = [("Backlog", []), ("Doing", [issue])]
    return project, board, columns


def scoped_label(project, name="type::bug", color="#DC143C", description="a defect"):
    """Give the project a non-column label and put it on its first issue."""
    label = types.SimpleNamespace(
        name=name, color=color, description=description, id=9, save=lambda: None
    )
    project.labels.list().append(label)
    project.issues.list()[0].labels.append(name)
    return label


def test_pulled_spec_plans_clean_against_its_own_board(monkeypatch):
    """pull then plan must be a no-op, or pull is lying about the board."""
    project, board, columns = board_fixture()
    scoped_label(project)
    spec = apply.spec_from_board(project, board, columns)
    spec.setdefault("issues", [])
    use_project(monkeypatch, project)
    assert apply.plan(None, spec) == []


def test_pull_carries_non_column_labels_with_colour_and_description():
    project, board, columns = board_fixture()
    scoped_label(project)
    scoped_label(project, "stale", "#808080", description=None)
    spec = apply.spec_from_board(project, board, columns)
    assert spec["labels"] == [
        {"name": "stale", "color": "gray"},
        {"name": "type::bug", "color": "crimson", "description": "a defect"},
    ]
    assert "labels" not in apply.spec_from_board(*board_fixture())


def test_pull_then_load_then_plan_is_still_clean(monkeypatch, tmp_path):
    project, board, columns = board_fixture()
    scoped_label(project)
    spec = reload(apply.spec_from_board(project, board, columns), tmp_path, "b.yaml")
    assert apply.plan(use_project(monkeypatch, project), spec) == []


def test_pulled_spec_drops_empty_fields():
    project, board, columns = board_fixture()
    spec = apply.spec_from_board(project, board, columns)
    assert spec["columns"] == [{"name": "Doing", "color": "gitlab blue"}]
    assert "assignee" not in spec["issues"][0]
    assert spec["issues"][0]["description"] == "body"
    assert spec["issues"][0]["iid"] == 7


# --- offline: diff against a pulled spec -----------------------------------


def reload(spec, tmp_path, name):
    f = tmp_path / name
    f.write_text(apply.dump(spec))
    return apply.load(str(f))


def test_pulled_spec_diffs_empty_against_itself(tmp_path):
    project, board, columns = board_fixture()
    spec = apply.spec_from_board(project, board, columns)
    base = reload(spec, tmp_path, "base.yaml")
    edited = reload(spec, tmp_path, "edited.yaml")
    assert apply.diff(edited, apply.have_from_spec(base)) == []


def test_offline_plan_spots_a_move_and_a_new_issue():
    base = {**SPEC, "issues": [{"title": "one", "labels": ["Doing"], "iid": 1}]}
    edited = {
        **SPEC,
        "issues": [{"title": "one", "labels": ["Blocked"], "iid": 1}, {"title": "two"}],
    }
    assert apply.diff(edited, apply.have_from_spec(base)) == [
        ("changed", "issue", "one: labels [Doing] -> [Blocked]"),
        ("added", "issue", "two"),
    ]


def test_unquoted_date_in_edited_spec_is_not_a_diff():
    base = {**SPEC, "issues": [{"title": "one", "due_date": "2026-09-01"}]}
    edited = {
        **SPEC,
        "issues": [{"title": "one", "due_date": datetime.date(2026, 9, 1)}],
    }
    assert apply.diff(edited, apply.have_from_spec(base)) == []


def test_diff_reports_assignee_by_username():
    base = {
        **SPEC,
        "issues": [{"title": "one", "labels": ["Doing"], "assignee": "alice"}],
    }
    assert apply.diff(SPEC, apply.have_from_spec(base)) == [
        ("changed", "issue", "one: assignee alice -> none")
    ]
    assert apply.diff(SPEC, apply.have_from_spec(SPEC)) == []


def fake_gl(users):
    """gl.users.list(username=...) over a {username: id} table."""
    return types.SimpleNamespace(
        users=types.SimpleNamespace(
            list=lambda username: (
                [types.SimpleNamespace(id=users[username])] if username in users else []
            )
        )
    )


def test_plan_explains_a_rejected_token_from_the_user_lookup(monkeypatch):
    """The users lookup runs first now; its 401 must not become a traceback."""
    import gitlab as gitlab_pkg

    def rejected(**_):
        raise gitlab_pkg.exceptions.GitlabAuthenticationError(
            response_code=401, error_message="invalid_token"
        )

    gl = types.SimpleNamespace(users=types.SimpleNamespace(list=rejected))
    spec = {**SPEC, "issues": [{"title": "one", "assignee": "root"}]}
    with pytest.raises(client.GitlabProblem, match="rejected the token"):
        apply.plan(gl, spec)


def test_ensure_issues_sends_assignee_ids_not_username():
    created = []
    project = FakeProject(issues=[])
    project.issues.create = lambda payload: created.append(payload) or FakeIssue("one")
    spec = {**SPEC, "issues": [{"title": "one", "assignee": "alice", "iid": 9}]}
    live = apply.ensure_issues(project, spec, lambda *_: None, {"alice": 42})
    assert created == [
        {
            "title": "one",
            "labels": [],
            "description": "",
            "due_date": None,
            "assignee_ids": [42],
        }
    ]
    assert live["one"].title == "one"  # created issues come back for ensure_notes


def test_plan_rejects_an_unknown_user_before_touching_the_project(monkeypatch):
    spec = {**SPEC, "issues": [{"title": "one", "assignee": "nobody"}]}
    use_project(monkeypatch, None)
    with pytest.raises(apply.SpecError, match="nobody"):
        apply.plan(fake_gl({}), spec)


def test_dump_load_roundtrip(tmp_path):
    project, board, columns = board_fixture()
    text = apply.dump(apply.spec_from_board(project, board, columns))
    f = tmp_path / "b.yaml"
    f.write_text(text)
    loaded = apply.load(str(f))
    assert loaded["issues"][0]["title"] == "one"
    assert loaded["issues"][0]["due_date"] == "2026-09-01"


# --- colors ----------------------------------------------------------------


def test_color_names_translate_to_hex():
    assert apply.norm_color("crimson") == "#dc143c"
    assert apply.norm_color("Rose-Red") == "#c21e56"
    assert apply.norm_color("MAGENTA_PINK") == "#cc338b"


def test_hex_passes_through_lowercased():
    assert apply.norm_color("#DC143C") == "#dc143c"
    assert apply.norm_color("#fff") == "#fff"


def test_unknown_color_is_a_spec_error():
    with pytest.raises(apply.SpecError, match="unknown color"):
        apply.norm_color("blurple")


def test_load_rejects_a_label_that_is_also_a_column(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text(
        "project: g/p\nboard: B\ncolumns:\n  - name: Doing\n"
        "labels:\n  - name: Doing\n    color: red\n"
    )
    with pytest.raises(apply.SpecError, match="'Doing' is a column"):
        apply.load(str(f))
    f.write_text("project: g/p\nboard: B\nlabels:\n  - color: red\n")
    with pytest.raises(apply.SpecError, match="needs a name"):
        apply.load(str(f))


def test_load_normalises_label_colors(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text(
        "project: g/p\nboard: B\nlabels:\n  - name: type::bug\n    color: crimson\n"
    )
    assert apply.load(str(f))["labels"] == [{"name": "type::bug", "color": "#dc143c"}]


def test_ensure_labels_creates_and_fixes_extra_labels():
    created = []
    project = writable_project(FakeIssue("one"))
    project.labels.create = lambda p: created.append(p) or types.SimpleNamespace(**p)
    stale = scoped_label(project, "stale", "#808080", description="old")
    changes = []
    extra = [
        {"name": "type::bug", "color": "#dc143c", "description": "a defect"},
        {"name": "stale", "color": "#808080", "description": "sat too long"},
        {"name": "Doing"},  # a column: already right, nothing written
    ]
    apply.ensure_labels(project, SPEC["columns"], lambda *c: changes.append(c), extra)
    assert created == [
        {"name": "type::bug", "color": "#dc143c", "description": "a defect"}
    ]
    assert stale.description == "sat too long"
    assert changes == [
        ("added", "label", "type::bug (#dc143c)"),
        ("changed", "label", "stale description"),
    ]


def test_plan_reports_a_missing_label_and_a_colour_change(monkeypatch):
    project = writable_project(FakeIssue("one", labels=["Doing"]))
    scoped_label(project, "stale", "#808080", description="old")
    spec = {
        **SPEC,
        "labels": [
            {"name": "type::bug", "color": "#dc143c"},
            {"name": "stale", "color": "red", "description": "old"},
        ],
    }
    gl = use_project(monkeypatch, project)
    assert apply.plan(gl, spec) == [
        ("added", "label", "type::bug"),
        ("changed", "label", "stale colour -> red"),
    ]
    assert apply.plan(gl, {**spec, "labels": [{"name": "stale"}]}) == []


def test_offline_diff_sees_a_base_label():
    base = {**SPEC, "labels": [{"name": "stale", "color": "#808080"}]}
    edited = {
        **SPEC,
        "labels": [
            {"name": "stale", "color": "gray", "description": "sat too long"},
            {"name": "type::bug"},
        ],
    }
    assert apply.diff(edited, apply.have_from_spec(base)) == [
        ("changed", "label", "stale description"),
        ("added", "label", "type::bug"),
    ]
    assert apply.diff(base, apply.have_from_spec(base)) == []
    assert ("added", "label", "stale") in apply.diff(base, None)


def test_load_normalises_column_colors(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text(
        "project: g/p\nboard: B\ncolumns:\n  - name: Doing\n    color: crimson\n"
    )
    assert apply.load(str(f))["columns"][0]["color"] == "#dc143c"


def test_pull_prefers_the_friendly_name():
    project, board, columns = board_fixture()
    project.labels.list()[0].color = "#DC143C"
    spec = apply.spec_from_board(project, board, columns)
    assert spec["columns"][0]["color"] == "crimson"


def test_cross_project_header_is_qualified(monkeypatch):
    projects = {
        "grp/a": types.SimpleNamespace(
            path_with_namespace="grp/a",
            issues=types.SimpleNamespace(
                get=lambda iid: types.SimpleNamespace(
                    iid=iid, notes=FakeNotes([fake_note("hello")])
                )
            ),
        ),
        "grp/b": types.SimpleNamespace(
            path_with_namespace="grp/b",
            issues=types.SimpleNamespace(get=lambda iid: projects_b_issue),
        ),
    }
    projects_b_issue = types.SimpleNamespace(iid=9, notes=FakeNotes())
    monkeypatch.setattr(apply.client, "get_project", lambda _gl, path: projects[path])
    n = apply.migrate_comments(None, "grp/a", 1, 9, dst_path="grp/b")
    assert n == 1
    assert projects_b_issue.notes.notes[0].body.startswith("*from grp/a#1,")


def test_same_project_dst_path_keeps_the_short_ref(monkeypatch):
    issues = migration_project(monkeypatch, [fake_note("hi")])
    apply.migrate_comments(None, "grp/proj", 1, 2, dst_path="grp/proj")
    assert issues[2].notes.notes[0].body.startswith("*from #1,")


def test_close_issue_notes_the_successors_and_closes(monkeypatch):
    issue = types.SimpleNamespace(iid=1, state="opened", notes=FakeNotes(), saved=False)
    issue.save = lambda: setattr(issue, "saved", True)
    project = types.SimpleNamespace(
        path_with_namespace="grp/proj",
        issues=types.SimpleNamespace(get=lambda _iid: issue),
    )
    use_project(monkeypatch, project)
    assert apply.close_issue(None, "grp/proj", 1, superseded_by=["#2", "x/y#3"])
    assert issue.notes.notes[0].body == "superseded by #2, x/y#3"
    assert issue.state_event == "close" and issue.saved


def test_close_issue_leaves_a_closed_issue_alone(monkeypatch):
    issue = types.SimpleNamespace(iid=1, state="closed", notes=FakeNotes())
    project = types.SimpleNamespace(
        path_with_namespace="grp/proj",
        issues=types.SimpleNamespace(get=lambda _iid: issue),
    )
    use_project(monkeypatch, project)
    assert apply.close_issue(None, "grp/proj", 1) is False
    assert not issue.notes.notes


def test_the_superseded_breadcrumb_is_not_migrated(monkeypatch):
    """close_issue's note on the source must not ride along to the successor."""
    issues = migration_project(
        monkeypatch, [fake_note("real"), fake_note("superseded by #2")]
    )
    assert apply.migrate_comments(None, "grp/proj", 1, 2) == 1
    assert len(issues[2].notes.notes) == 1


# --- cli: the offline paths never open a connection -------------------------


def test_offline_plan_and_show_never_open_gitlab(monkeypatch, tmp_path):
    from typer.testing import CliRunner

    from gitboard import cli, client

    def boom(*_, **__):
        raise AssertionError("client.gitlab() called on an offline path")

    monkeypatch.setattr(client, "gitlab", boom)
    base, edited = tmp_path / "b.yaml", tmp_path / "e.yaml"
    base.write_text(apply.dump({**SPEC, "issues": [{"title": "one", "iid": 1}]}))
    edited.write_text(
        apply.dump(
            {**SPEC, "issues": [{"title": "one", "iid": 1, "labels": ["Doing"]}]}
        )
    )
    runner = CliRunner()
    res = runner.invoke(cli.app, ["plan", str(edited), "--against", str(base)])
    assert res.exit_code == 0, res.output
    res = runner.invoke(cli.app, ["show", "--from", str(edited), "--markdown"])
    assert res.exit_code == 0, res.output
    assert "- #1 one — @unassigned" in res.output


# --- notes: the board as the conversation ----------------------------------


def noted_issue(title, notes=(), **kw):
    issue = FakeIssue(title, **kw)
    issue.notes = FakeNotes([fake_note(b) for b in notes])
    return issue


def test_pull_with_notes_carries_the_discussion_but_not_system_notes():
    project, board, columns = board_fixture()
    issue = columns[1][1][0]
    issue.notes = FakeNotes(
        [
            fake_note("added label ~Doing", system=True),
            fake_note("looks wrong\n", author="bob", created="2026-08-03T09:00:00Z"),
        ]
    )
    spec = apply.spec_from_board(project, board, columns, notes=True)
    assert spec["issues"][0]["discussion"] == [
        {"by": "bob", "at": "2026-08-03", "body": "looks wrong"}
    ]
    assert (
        "discussion" not in apply.spec_from_board(project, board, columns)["issues"][0]
    )


def writable_project(issue):
    """A FakeProject with enough surface for apply() itself to run through:
    labels with colors, a board whose lists already hold the columns."""
    project = FakeProject(
        labels=["Doing", "Blocked"], boards=["Dev Board"], issues=[issue]
    )
    for label in project.labels.list():
        label.color, label.id = "#428bca", 1
    project.boards.list()[0].id = 1
    lists = [types.SimpleNamespace(label={"name": n}) for n in ("Doing", "Blocked")]
    board = types.SimpleNamespace(id=1, name="Dev Board", lists=_lister(lists))
    project.boards.get = lambda _id: board
    return project


def test_staged_note_is_planned_online_and_posted_once(monkeypatch):
    issue = noted_issue("one", labels=["Doing"], notes=["old comment"])
    spec = {
        **SPEC,
        "issues": [{"title": "one", "labels": ["Doing"], "notes": ["reply\n"]}],
    }
    gl = use_project(monkeypatch, writable_project(issue))
    assert apply.plan(gl, spec) == [("added", "note", "one: reply")]
    assert apply.apply(gl, spec) == [("added", "note", "one: reply")]
    assert [n.body for n in issue.notes.notes] == [
        "old comment",
        "*staged via gitboard*\n\nreply",
    ]
    assert apply.plan(gl, spec) == []
    assert apply.apply(gl, spec) == []


def test_staged_note_offline_skips_what_the_base_discussion_already_has():
    """A pulled discussion body carries the marker a previous apply wrote;
    a person's own "seen" is not the same note and would be posted."""
    base = {
        **SPEC,
        "issues": [
            {
                "title": "one",
                "labels": ["Doing"],
                "discussion": [
                    {"by": "bob", "at": "2026-08-03", "body": apply.marked("seen")}
                ],
            }
        ],
    }
    edited = {
        **SPEC,
        "issues": [{"title": "one", "labels": ["Doing"], "notes": ["seen", "new"]}],
    }
    assert apply.diff(edited, apply.have_from_spec(base)) == [
        ("added", "note", "one: new")
    ]


def test_notes_on_a_new_issue_are_planned_with_it():
    spec = {**SPEC, "issues": [{"title": "fresh", "notes": ["hello"]}]}
    assert apply.diff(spec, apply.have_from_spec(SPEC)) == [
        ("added", "issue", "fresh"),
        ("added", "note", "fresh: hello"),
    ]
    assert ("added", "note", "fresh: hello") in apply.diff(spec, None)


# --- closed issues, UI labels, matching ------------------------------------


def test_a_closed_title_is_skipped_not_recreated(monkeypatch):
    closed = FakeIssue("one", labels=["Doing"], state="closed", notes=["old"])
    spec = {**SPEC, "issues": [{"title": "one", "labels": ["Doing"], "notes": ["hi"]}]}
    project = writable_project(closed)
    gl = use_project(monkeypatch, project)
    assert apply.plan(gl, spec) == [("skipped", "issue", "one: closed on GitLab")]
    assert apply.apply(gl, spec) == [("skipped", "issue", "one: closed on GitLab")]
    assert len(project.issues.list()) == 1 and len(closed.notes.notes) == 1


def test_an_open_match_beats_a_closed_one(monkeypatch):
    project = writable_project(FakeIssue("one", labels=["Doing"], state="closed"))
    project.issues.list().append(FakeIssue("one", labels=["Doing"], iid=2))
    assert apply.plan(use_project(monkeypatch, project), SPEC) == []


def test_a_ui_added_label_survives_a_move(monkeypatch):
    issue = FakeIssue("one", labels=["Doing", "bug"])
    spec = {**SPEC, "issues": [{"title": "one", "labels": ["Blocked"]}]}
    gl = use_project(monkeypatch, writable_project(issue))
    assert apply.plan(gl, spec) == [
        ("changed", "issue", "one: labels [Doing] -> [Blocked]")
    ]
    apply.apply(gl, spec)
    assert issue.saved and issue.labels == ["Blocked", "bug"]
    assert apply.plan(gl, spec) == []


def test_a_ui_added_label_is_not_a_diff_offline():
    base = {**SPEC, "issues": [{"title": "one", "labels": ["Doing", "bug"]}]}
    assert apply.diff(SPEC, apply.have_from_spec(base)) == []


def test_two_open_issues_with_one_title_is_an_error(monkeypatch):
    project = writable_project(FakeIssue("one", iid=1))
    project.issues.list().append(FakeIssue("one", iid=2))
    with pytest.raises(apply.SpecError, match="two open issues titled 'one'"):
        apply.plan(use_project(monkeypatch, project), SPEC)


def test_load_rejects_duplicate_titles(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text("project: g/p\nboard: B\nissues:\n  - title: One\n  - title: 'one '\n")
    with pytest.raises(apply.SpecError, match="duplicate issue title"):
        apply.load(str(f))


def test_load_strips_titles_and_a_trailing_space_still_matches(monkeypatch, tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text("project: g/p\nboard: Dev Board\nissues:\n  - title: 'one '\n")
    spec = apply.load(str(f))
    assert spec["issues"][0]["title"] == "one"
    project = writable_project(FakeIssue("one "))
    assert apply.plan(use_project(monkeypatch, project), spec) == []


def test_an_iid_match_makes_a_retitle_a_rename(monkeypatch):
    issue = FakeIssue("old name", labels=["Doing"], iid=7)
    spec = {**SPEC, "issues": [{"title": "new name", "labels": ["Doing"], "iid": 7}]}
    gl = use_project(monkeypatch, writable_project(issue))
    assert apply.plan(gl, spec) == [("changed", "issue", "old name: title -> new name")]
    assert apply.apply(gl, spec) == [
        ("changed", "issue", "old name: title -> new name")
    ]
    assert issue.title == "new name" and issue.saved


def test_notes_land_on_a_freshly_created_issue(monkeypatch):
    spec = {**SPEC, "issues": [{"title": "fresh", "notes": ["hello"]}]}
    project = writable_project(FakeIssue("other"))
    gl = use_project(monkeypatch, project)
    assert apply.apply(gl, spec) == [
        ("added", "issue", "fresh"),
        ("added", "note", "fresh: hello"),
    ]
    fresh = project.issues.list()[-1]
    assert [n.body for n in fresh.notes.notes] == ["*staged via gitboard*\n\nhello"]


def test_marker_is_not_doubled_and_is_idempotent_offline():
    body = apply.marked("x")
    assert apply.marked(body) == body
    base = {**SPEC, "issues": [{"title": "one", "notes": ["x"]}]}
    edited = {**SPEC, "issues": [{"title": "one", "notes": [body, "y"]}]}
    assert apply.diff(edited, apply.have_from_spec(base)) == [
        ("added", "note", "one: y")
    ]


# --- detail strings ----------------------------------------------------------


def test_changed_fields_read_old_to_new_one_line_each():
    base = {
        **SPEC,
        "issues": [
            {
                "title": "one",
                "assignee": "alice",
                "due_date": "2026-09-01",
                "description": "long body",
            }
        ],
    }
    edited = {
        **SPEC,
        "issues": [
            {
                "title": "one",
                "assignee": "bob",
                "due_date": datetime.date(2026, 9, 2),
                "description": "longer body",
            }
        ],
    }
    assert apply.diff(edited, apply.have_from_spec(base)) == [
        ("changed", "issue", "one: description (edited)"),
        ("changed", "issue", "one: due_date 2026-09-01 -> 2026-09-02"),
        ("changed", "issue", "one: assignee alice -> bob"),
    ]


# --- three-way: the .base from pull --base ---------------------------------


def three_way(edited_labels, live_labels, old_labels=("Doing",), force=False):
    """diff() on one issue whose labels are `old` in the base, `live` on
    GitLab, and `edited` in the YAML."""
    columns = [{"name": n} for n in ("Doing", "Blocked", "Done")]

    def mk(labels):
        return {
            **SPEC,
            "columns": columns,
            "issues": [{"title": "one", "labels": list(labels)}],
        }

    live = apply.have_from_spec(mk(live_labels))
    return apply.diff(mk(edited_labels), live, base=mk(old_labels), force=force)


def test_untouched_field_changed_on_gitlab_is_kept():
    assert three_way(edited_labels=["Doing"], live_labels=["Blocked"]) == [
        ("skipped", "issue", "one: labels changed on GitLab, kept")
    ]


def test_edited_field_unchanged_on_gitlab_is_written():
    assert three_way(edited_labels=["Blocked"], live_labels=["Doing"]) == [
        ("changed", "issue", "one: labels [Doing] -> [Blocked]")
    ]


def test_edited_field_already_matching_gitlab_is_quiet():
    assert three_way(edited_labels=["Blocked"], live_labels=["Blocked"]) == []


def test_both_sides_changed_is_drift_unless_forced():
    assert three_way(edited_labels=["Blocked"], live_labels=["Done"]) == [
        ("drift", "issue", "one: labels [Done] -> [Blocked]")
    ]
    assert three_way(edited_labels=["Blocked"], live_labels=["Done"], force=True) == [
        ("changed", "issue", "one: labels [Done] -> [Blocked]")
    ]


def test_apply_with_base_writes_only_what_the_yaml_changed(monkeypatch):
    """Agent moved the card; a person reassigned it meanwhile. Both survive."""
    issue = FakeIssue("one", labels=["Doing"], assignee="carol", iid=1)
    base = {**SPEC, "issues": [{"title": "one", "labels": ["Doing"], "iid": 1}]}
    edited = {**SPEC, "issues": [{"title": "one", "labels": ["Blocked"], "iid": 1}]}
    gl = use_project(monkeypatch, writable_project(issue))
    assert apply.apply(gl, edited, base=base) == [
        ("changed", "issue", "one: labels [Doing] -> [Blocked]"),
        ("skipped", "issue", "one: assignee changed on GitLab, kept"),
    ]
    assert issue.labels == ["Blocked"] and not hasattr(issue, "assignee_ids")


def test_an_issue_missing_from_the_base_is_diffed_two_way():
    base = {**SPEC, "issues": []}
    edited = {**SPEC, "issues": [{"title": "one", "labels": ["Blocked"]}]}
    live = apply.have_from_spec({**SPEC, "issues": [{"title": "one"}]})
    assert apply.diff(edited, live, base=base) == [
        ("changed", "issue", "one: labels [] -> [Blocked]")
    ]


# --- blockers and milestones ------------------------------------------------


def write_yaml(tmp_path, text):
    f = tmp_path / "b.yaml"
    f.write_text("project: grp/proj\nboard: B\n" + text)
    return str(f)


def test_load_rejects_blocked_by_cycle(tmp_path):
    path = write_yaml(
        tmp_path,
        "issues:\n"
        "  - {title: a, iid: 1, blocked_by: [2]}\n"
        "  - {title: b, iid: 2, blocked_by: [a]}\n",
    )
    with pytest.raises(apply.SpecError, match="b.yaml: blocked_by cycle"):
        apply.load(path)
    # a scalar is a typo, not one ref: a string would be iterated per letter
    path = write_yaml(tmp_path, "issues:\n  - {title: a, blocked_by: 9}\n")
    with pytest.raises(apply.SpecError, match="blocked_by must be a list"):
        apply.load(path)


def test_load_rejects_unknown_milestone(tmp_path):
    path = write_yaml(tmp_path, "issues:\n  - {title: t, milestone: X}\n")
    with pytest.raises(
        apply.SpecError, match=r"milestone 'X' \(issue 't'\) is not under milestones:"
    ):
        apply.load(path)
    path = write_yaml(tmp_path, "milestones:\n  - {due_date: 2026-11-01}\n")
    with pytest.raises(apply.SpecError, match="needs a title"):
        apply.load(path)
    path = write_yaml(tmp_path, "milestones:\n  - {title: X}\n  - {title: 'X '}\n")
    with pytest.raises(apply.SpecError, match="duplicate milestone 'X'"):
        apply.load(path)
    path = write_yaml(
        tmp_path, "milestones:\n  - {title: X}\nissues:\n  - {title: t, milestone: X}\n"
    )
    assert apply.load(path)["issues"][0]["milestone"] == "X"


def test_load_rejects_two_priorities(tmp_path):
    path = write_yaml(
        tmp_path, "issues:\n  - {title: t, labels: ['priority::1', 'priority::2']}\n"
    )
    with pytest.raises(apply.SpecError, match="two priority:: labels"):
        apply.load(path)


def test_absent_blocked_by_is_unmanaged(monkeypatch):
    """A hand-written spec must not wipe every link on the board."""
    issue = FakeIssue("one", labels=["Doing"], links=[link(9)])
    gl = use_project(monkeypatch, writable_project(issue))
    assert apply.plan(gl, SPEC) == []
    assert apply.apply(gl, SPEC) == []
    assert issue.links.created == issue.links.deleted == []
    assert issue.links.listed == 0  # no key, no links request


def test_empty_blocked_by_removes_native_links(monkeypatch):
    issue = FakeIssue("one", labels=["Doing"], links=[link(9, link_id=5)])
    spec = {**SPEC, "issues": [{"title": "one", "labels": ["Doing"], "blocked_by": []}]}
    gl = use_project(monkeypatch, writable_project(issue))
    want = [("changed", "issue", "one: blocked_by [#9] -> []")]
    assert apply.plan(gl, spec) == want
    assert apply.apply(gl, spec) == want
    assert issue.links.deleted == [5]
    assert apply.plan(gl, spec) == []


FOOTER_SKIP = ("skipped", "link", "one: blocker #9 is a footer ref — edit the description")


FOOTED = {"title": "one", "labels": ["Doing"], "description": "x\n\nBlocked by: #9"}


def test_dropping_a_footer_only_blocker_is_one_skip_not_a_change(monkeypatch):
    """gb-219: the ref cannot be removed, so plan must not call it a change
    forever; it says skipped, once, in plan and again (once) in apply."""
    issue = FakeIssue("one", labels=["Doing"], description="x\n\nBlocked by: #9")
    spec = {**SPEC, "issues": [{**FOOTED, "blocked_by": []}]}
    gl = use_project(monkeypatch, writable_project(issue))
    assert apply.plan(gl, spec) == [FOOTER_SKIP]
    assert apply.apply(gl, spec) == [FOOTER_SKIP]
    assert issue.links.deleted == []


def test_footer_skip_recorded_once_when_other_blockers_change(monkeypatch):
    issue = FakeIssue("one", labels=["Doing"], description="x\n\nBlocked by: #9")
    spec = {**SPEC, "issues": [{**FOOTED, "blocked_by": [11]}]}
    gl = use_project(monkeypatch, writable_project(issue))
    want = [
        ("changed", "issue", "one: blocked_by [] -> [#11]"),
        FOOTER_SKIP,
    ]
    assert sorted(apply.plan(gl, spec)) == sorted(want)
    assert sorted(apply.apply(gl, spec)) == sorted(want)
    assert [c["target_issue_iid"] for c in issue.links.created] == [11]


def test_blocked_by_three_way_skipped_and_drift():
    def bb(*refs):
        return {"blocked_by": list(refs)}

    assert apply.issue_changes("one", bb("9"), bb("9", "11"), bb("9"), set()) == (
        {},
        [("skipped", "issue", "one: blocked_by changed on GitLab, kept")],
    )
    assert apply.issue_changes("one", bb("10"), bb("11"), bb("9"), set()) == (
        {},
        [("drift", "issue", "one: blocked_by [#11] -> [#10]")],
    )
    # a base card that lacked the key had no blockers
    assert apply.issue_changes("one", bb("10"), bb(), {"labels": []}, set()) == (
        {"blocked_by": ["10"]},
        [("changed", "issue", "one: blocked_by [] -> [#10]")],
    )


def test_new_title_ref_links_after_create(monkeypatch):
    a = FakeIssue("A", iid=1)
    spec = {
        **SPEC,
        "issues": [{"title": "A", "iid": 1, "blocked_by": ["B"]}, {"title": "B"}],
    }
    project = writable_project(a)
    gl = use_project(monkeypatch, project)
    changed = ("changed", "issue", "A: blocked_by [] -> [new:B]")
    assert apply.plan(gl, spec) == [changed, ("added", "issue", "B")]
    assert apply.apply(gl, spec) == [changed, ("added", "issue", "B")]
    b = project.issues.list()[-1]
    assert b.title == "B" and not hasattr(b, "blocked_by")
    assert a.links.created == [
        {
            "target_project_id": 1,
            "target_issue_iid": b.iid,
            "link_type": "is_blocked_by",
        }
    ]
    # B is live now: the title ref resolves to its iid, so a YAML not yet
    # re-pulled shows neither a phantom change nor drift against its base
    assert apply.plan(gl, spec) == []
    base = {**SPEC, "issues": [{"title": "A", "iid": 1}]}
    assert apply.plan(gl, spec, base=base) == []


def test_milestone_created_and_assigned(monkeypatch):
    issue = FakeIssue("one", labels=["Doing"])
    project = writable_project(issue)
    spec = {
        **SPEC,
        "milestones": [{"title": "Beta", "due_date": datetime.date(2026, 11, 1)}],
        "issues": [{"title": "one", "labels": ["Doing"], "milestone": "Beta"}],
    }
    gl = use_project(monkeypatch, project)
    want = [
        ("added", "milestone", "Beta"),
        ("changed", "issue", "one: milestone none -> Beta"),
    ]
    assert apply.plan(gl, spec) == want
    assert apply.apply(gl, spec) == want
    (made,) = project.milestones.list()
    assert made.created == {"title": "Beta", "due_date": "2026-11-01"}
    assert issue.milestone_id == made.id and issue.saved


def test_milestone_clear_with_null(monkeypatch):
    beta = {"title": "Beta", "due_date": None, "description": ""}
    issue = FakeIssue("one", labels=["Doing"], milestone=beta)
    project = writable_project(issue)
    project.milestones.list().append(milestone("Beta"))
    spec = {
        **SPEC,
        "issues": [{"title": "one", "labels": ["Doing"], "milestone": None}],
    }
    gl = use_project(monkeypatch, project)
    want = [("changed", "issue", "one: milestone Beta -> none")]
    assert apply.plan(gl, spec) == want
    assert apply.apply(gl, spec) == want
    assert issue.milestone_id is None and issue.saved
    # no key: the milestone is left alone
    assert apply.plan(gl, SPEC) == []


def test_plan_reports_milestone_changes(monkeypatch):
    beta = milestone("Beta", "2026-11-01", "old")
    project = writable_project(FakeIssue("one", labels=["Doing"]))
    project.milestones.list().append(beta)
    spec = {
        **SPEC,
        "milestones": [
            {
                "title": "Beta",
                "due_date": datetime.date(2026, 11, 15),
                "description": "new",
            },
            {"title": "Gamma"},
        ],
    }
    want = [
        ("changed", "milestone", "Beta: due_date 2026-11-01 -> 2026-11-15"),
        ("changed", "milestone", "Beta: description (edited)"),
        ("added", "milestone", "Gamma"),
    ]
    gl = use_project(monkeypatch, project)
    assert apply.plan(gl, spec) == want
    base = {**SPEC, "milestones": [{"title": "Beta", "due_date": "2026-11-01"}]}
    assert apply.diff(spec, apply.have_from_spec(base))[-1] == want[-1]
    assert ("added", "milestone", "Gamma") in apply.diff(spec, None)
    assert apply.apply(gl, spec) == want
    assert beta.saved and beta.due_date == "2026-11-15" and beta.description == "new"
    assert apply.plan(gl, spec) == []


def test_group_milestone_counts_as_existing(monkeypatch):
    issue = FakeIssue("one", labels=["Doing"])
    project = writable_project(issue)
    project.namespace = {"kind": "group", "full_path": "grp"}
    group = types.SimpleNamespace(milestones=_lister([milestone("Beta", id=77)]))
    spec = {
        **SPEC,
        "milestones": [{"title": "Beta"}],
        "issues": [{"title": "one", "labels": ["Doing"], "milestone": "Beta"}],
    }
    gl = use_project(monkeypatch, project)
    gl.groups = types.SimpleNamespace(get=lambda path: group)
    want = [("changed", "issue", "one: milestone none -> Beta")]
    assert apply.plan(gl, spec) == want
    assert apply.apply(gl, spec) == want
    assert project.milestones.list() == [] and issue.milestone_id == 77

    def forbidden(path):
        raise GitlabGetError("403 Forbidden", response_code=403)

    gl.groups.get = forbidden  # unreadable group: project milestones only
    assert apply.plan(gl, spec)[0] == ("added", "milestone", "Beta")


def test_pull_then_plan_is_empty_with_links_milestones_priority(monkeypatch, tmp_path):
    project, board, columns = board_fixture()
    issue = project.issues.list()[0]
    issue.description = "body\n\nBlocked by: #11"
    issue.milestone = {
        "title": "Beta",
        "due_date": "2026-11-01",
        "description": "what beta means\n",
    }
    issue.links = FakeLinks(
        [link(9), link(4, project_id=8, link_id=2, full="infra/platform#4")]
    )
    project.milestones.list().append(
        milestone("Beta", "2026-11-01", "what beta means\n")
    )
    scoped_label(project, "priority::2", "#ff0000", None)
    spec = apply.spec_from_board(project, board, columns)
    assert spec["issues"][0]["blocked_by"] == [9, 11, "infra/platform#4"]
    assert spec["issues"][0]["milestone"] == "Beta"
    assert spec["milestones"] == [
        {"title": "Beta", "due_date": "2026-11-01", "description": "what beta means"}
    ]
    spec = reload(spec, tmp_path, "b.yaml")
    assert apply.plan(use_project(monkeypatch, project), spec) == []
    assert apply.diff(spec, apply.have_from_spec(spec)) == []


def test_pull_keeps_cardless_active_milestone_not_closed(monkeypatch, tmp_path):
    project, board, columns = board_fixture()
    old = milestone("Old", "2026-01-01", id=52)
    old.state = "closed"
    project.milestones.list().extend(
        [milestone("Later", "2026-12-01", "horizon\n", id=51), old]
    )
    spec = apply.spec_from_board(project, board, columns)
    assert spec["milestones"] == [
        {"title": "Later", "due_date": "2026-12-01", "description": "horizon"}
    ]
    spec = reload(spec, tmp_path, "b.yaml")
    assert apply.plan(use_project(monkeypatch, project), spec) == []
    assert apply.diff(spec, apply.have_from_spec(spec)) == []


def test_blocker_not_found_is_skipped_and_rest_applies(monkeypatch):
    issue = FakeIssue("one", labels=["Doing"])
    project = writable_project(issue)

    def get_project(_gl, path):
        if path != "grp/proj":
            raise client.GitlabProblem(f"no project {path!r}")
        return project

    monkeypatch.setattr(apply.client, "get_project", get_project)
    spec = {
        **SPEC,
        "issues": [
            {"title": "one", "labels": ["Doing"], "blocked_by": [9, "gone/x#4"]}
        ],
    }
    assert apply.apply(types.SimpleNamespace(), spec) == [
        ("changed", "issue", "one: blocked_by [] -> [#9, gone/x#4]"),
        ("skipped", "link", "one: blocker gone/x#4 not found"),
    ]
    assert [c["target_issue_iid"] for c in issue.links.created] == [9]


def test_downgrade_raises_one_problem(monkeypatch):
    first = FakeIssue("one", iid=1)
    second = FakeIssue("two", iid=2)
    first.links.downgrade = second.links.downgrade = True
    project = writable_project(first)
    project.issues.list().append(second)
    spec = {
        **SPEC,
        "issues": [
            {"title": "one", "iid": 1, "blocked_by": [9]},
            {"title": "two", "iid": 2, "blocked_by": [9]},
        ],
    }
    gl = use_project(monkeypatch, project)
    with pytest.raises(client.GitlabProblem, match="Premium"):
        apply.apply(gl, spec)
    assert first.links.items == [] and len(first.links.deleted) == 1
    assert second.links.created == []  # the first refusal stopped the run


# --- board order -------------------------------------------------------------


def ordered_project(iids, closed=()):
    """A writable project whose open issues sit in `iids` board order. Each
    issue's `reorder` moves it the way GitLab does, so a re-read sees it."""
    issues = [FakeIssue(f"card {n}", labels=["Doing"], iid=n) for n in iids]
    issues += [FakeIssue(f"card {n}", iid=n, state="closed") for n in closed]
    project = writable_project(issues[0])
    project.issues.list()[1:] = issues[1:]
    project.reorders = []

    def renumber(order):
        for at, issue in enumerate(order):
            issue.relative_position = at * 10

    def reorder_of(issue):
        def reorder(move_after_id=None, move_before_id=None):
            project.reorders.append((issue.iid, move_after_id, move_before_id))
            # GitLab: move_after_id names the card that ends up *after* this
            # one, move_before_id the card that ends up before it
            order = [i for i in board_order(project) if i is not issue]
            target = move_after_id or move_before_id
            at = next(n for n, i in enumerate(order) if i.id == target)
            order.insert(at if move_after_id else at + 1, issue)
            renumber(order)

        return reorder

    for issue in issues:
        issue.reorder = reorder_of(issue)
    renumber([i for i in issues if i.state == "opened"])
    return project


def board_order(project):
    opened = [i for i in project.issues.list() if i.state == "opened"]
    return sorted(opened, key=lambda i: i.relative_position)


def order_spec(iids):
    return {
        **SPEC,
        "issues": [{"title": f"card {n}", "labels": ["Doing"], "iid": n} for n in iids],
    }


def test_pull_writes_issues_in_board_order():
    project = ordered_project([3, 1, 2])
    project.issues.list()[1].relative_position = None  # #1 never dragged
    columns = [("Doing", project.issues.list())]
    board = types.SimpleNamespace(name="Dev Board")
    spec = apply.spec_from_board(project, board, columns)
    assert [i["iid"] for i in spec["issues"]] == [3, 2, 1]


def test_order_unmanaged_without_base(monkeypatch):
    """A hand-written YAML never reshuffles the board."""
    project = ordered_project([1, 2, 3, 4])
    gl = use_project(monkeypatch, project)
    spec = order_spec([4, 1, 2, 3])
    assert apply.plan(gl, spec) == []
    assert apply.apply(gl, spec) == []
    assert project.reorders == []


def test_order_write_uses_minimal_moves(monkeypatch):
    project = ordered_project([1, 2, 3, 4])
    gl = use_project(monkeypatch, project)
    base, spec = order_spec([1, 2, 3, 4]), order_spec([4, 1, 2, 3])
    assert apply.plan(gl, spec, base=base) == [("changed", "order", "#4 to the top")]
    assert apply.apply(gl, spec, base=base) == [("changed", "order", "#4 to the top")]
    assert project.reorders == [(4, 1001, None)]  # #1 placed after it, global id
    assert [i.iid for i in board_order(project)] == [4, 1, 2, 3]
    assert apply.plan(gl, spec, base=spec) == []


def test_order_skipped_when_only_gitlab_moved(monkeypatch):
    project = ordered_project([2, 1, 3])
    gl = use_project(monkeypatch, project)
    spec = order_spec([1, 2, 3])
    kept = [("skipped", "order", "board order changed on GitLab, kept")]
    assert apply.plan(gl, spec, base=spec) == kept
    assert apply.apply(gl, spec, base=spec) == kept
    assert project.reorders == []


def test_order_drift_refused_then_forced(monkeypatch):
    project = ordered_project([2, 1, 3])
    gl = use_project(monkeypatch, project)
    base, spec = order_spec([1, 2, 3]), order_spec([3, 1, 2])
    drift = [("drift", "order", "board order changed on GitLab and in the YAML")]
    assert apply.plan(gl, spec, base=base) == drift
    assert apply.apply(gl, spec, base=base) == drift
    assert project.reorders == []
    offline = apply.have_from_spec(order_spec([2, 1, 3]))
    assert apply.diff(spec, offline, base) == drift
    assert apply.apply(gl, spec, base=base, force=True) == [
        ("changed", "order", "#3 to the top"),
        ("changed", "order", "#1 after #3"),
    ]
    assert [i.iid for i in board_order(project)] == [3, 1, 2]


def test_pull_then_plan_is_empty_with_order(monkeypatch, tmp_path):
    project = ordered_project([3, 1, 2], closed=[5])
    columns = [("Doing", board_order(project))]
    board = types.SimpleNamespace(name="Dev Board")
    spec = reload(apply.spec_from_board(project, board, columns), tmp_path, "b.yaml")
    assert [i["iid"] for i in spec["issues"]] == [3, 1, 2]
    gl = use_project(monkeypatch, project)
    assert apply.plan(gl, spec, base=spec) == []
    assert apply.diff(spec, apply.have_from_spec(spec), base=spec) == []


def test_order_rerun_after_partial_failure(monkeypatch):
    """GitLab moves one card per call: the first move lands, the second
    fails. A plain rerun against the same base sees where the write stopped
    and finishes it; it is not drift."""
    project = ordered_project([1, 2, 3, 4, 5])
    gl = use_project(monkeypatch, project)
    base, spec = order_spec([1, 2, 3, 4, 5]), order_spec([5, 4, 1, 2, 3])
    card4 = project.issues.list()[3]
    real = card4.reorder

    def broken(**_):
        raise GitlabGetError("boom", response_code=500)

    card4.reorder = broken
    with pytest.raises(client.GitlabProblem, match="#4"):
        apply.apply(gl, spec, base=base)
    assert [i.iid for i in board_order(project)] == [5, 1, 2, 3, 4]
    card4.reorder = real
    rest = [("changed", "order", "#4 after #5")]
    assert apply.plan(gl, spec, base=base) == rest
    assert apply.apply(gl, spec, base=base) == rest
    assert [i.iid for i in board_order(project)] == [5, 4, 1, 2, 3]


def test_offline_diff_shows_a_yaml_reorder():
    """plan --against / status / pull --force read the .base as the live
    board: a reorder staged offline is an edit, not invisible."""
    base, spec = order_spec([1, 2, 3]), order_spec([3, 1, 2])
    assert apply.diff(spec, apply.have_from_spec(base)) == [
        ("changed", "order", "#3 to the top")
    ]
    assert apply.diff(base, apply.have_from_spec(base)) == []
