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
        self, title, labels=(), description="", due_date=None, assignee=None, iid=1
    ):
        self.title, self.labels, self.iid = title, list(labels), iid
        self.description, self.due_date = description, due_date
        self.assignee = {"username": assignee} if assignee else None


class FakeProject:
    def __init__(self, labels=(), boards=(), issues=()):
        self.labels = _lister([types.SimpleNamespace(name=n) for n in labels])
        self.boards = _lister([types.SimpleNamespace(name=n) for n in boards])
        self.issues = _lister(list(issues))


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
    assert pending == [("changed", "issue", "one: labels")]


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
        ("changed", "issue", "one: labels"),
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
        ("changed", "issue", "one: assignee")
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
    project.issues.create = created.append
    issues = [{"title": "one", "assignee": "alice", "iid": 9}]
    apply.ensure_issues(project, issues, lambda *_: None, {"alice": 42})
    assert created == [
        {
            "title": "one",
            "labels": [],
            "description": "",
            "due_date": None,
            "assignee_ids": [42],
        }
    ]


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

