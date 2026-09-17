"""Tests for apply.py. No network — the GitLab API surface is faked.

The two bugs these pin both cost a real debugging round: YAML parses an
unquoted date into a date object (not JSON-serialisable), and GitLab strips
the trailing newline that YAML's `|` preserves (phantom diff on every run).
"""

import datetime
import types

import pytest

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
    ):
        self.title, self.labels, self.iid = title, list(labels), iid
        self.description, self.due_date = description, due_date
        self.assignee = {"username": assignee} if assignee else None
        self.state, self.saved = state, False
        self.notes = FakeNotes([fake_note(b) for b in notes])

    def save(self):
        self.saved = True


class FakeProject:
    def __init__(self, labels=(), boards=(), issues=()):
        self.labels = _lister([types.SimpleNamespace(name=n) for n in labels])
        self.boards = _lister([types.SimpleNamespace(name=n) for n in boards])
        self.issues = _lister(list(issues))
        self.issues.create = self._create

    def _create(self, payload):
        items = self.issues.list()
        issue = FakeIssue(payload["title"], iid=len(items) + 100)
        for k, v in payload.items():
            setattr(issue, k, v)
        items.append(issue)
        return issue


def _lister(items):
    return types.SimpleNamespace(list=lambda **_: items)


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
    spec = {"title": "t", "labels": ["Doing"], "description": "body\n"}
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


def test_pulled_spec_plans_clean_against_its_own_board(monkeypatch):
    """pull then plan must be a no-op, or pull is lying about the board."""
    project, board, columns = board_fixture()
    spec = apply.spec_from_board(project, board, columns)
    spec.setdefault("issues", [])
    use_project(monkeypatch, project)
    assert apply.plan(None, spec) == []


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
