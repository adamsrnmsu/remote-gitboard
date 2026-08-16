"""Tests for apply.py. No network — the GitLab API surface is faked.

The two bugs these pin both cost a real debugging round: YAML parses an
unquoted date into a date object (not JSON-serialisable), and GitLab strips
the trailing newline that YAML's `|` preserves (phantom diff on every run).
"""

import datetime
import types

import pytest

import apply


class FakeIssue:
    def __init__(self, title, labels=(), description="", due_date=None, assignees=()):
        self.title, self.labels = title, list(labels)
        self.description, self.due_date = description, due_date
        self.assignees = [{"id": a} for a in assignees]


class FakeProject:
    def __init__(self, labels=(), boards=(), issues=()):
        self.labels = _lister([types.SimpleNamespace(name=n) for n in labels])
        self.boards = _lister([types.SimpleNamespace(name=n) for n in boards])
        self.issues = _lister(list(issues))


def _lister(items):
    return types.SimpleNamespace(list=lambda **_: items)


def fake_gl(project=None):
    def get(_path):
        if project is None:
            raise RuntimeError("404")
        return project

    return types.SimpleNamespace(projects=types.SimpleNamespace(get=get))


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
    want = apply.wanted_issue(
        None, {"title": "t", "due_date": datetime.date(2026, 9, 1)}
    )
    assert want["due_date"] == "2026-09-01"


def test_quoted_date_passes_through_unchanged():
    want = apply.wanted_issue(None, {"title": "t", "due_date": "2026-09-01"})
    assert want["due_date"] == "2026-09-01"


def test_labels_are_sorted_so_yaml_order_is_not_a_diff():
    a = apply.wanted_issue(None, {"title": "t", "labels": ["b", "a"]})
    b = apply.wanted_issue(None, {"title": "t", "labels": ["a", "b"]})
    assert a["labels"] == b["labels"]


def test_a_settled_issue_shows_no_drift():
    """wanted_issue and current_issue must agree, or apply is never idempotent."""
    spec = {"title": "t", "labels": ["Doing"], "description": "body\n"}
    issue = FakeIssue("t", labels=["Doing"], description="body")
    assert apply.wanted_issue(None, spec) == apply.current_issue(issue)


# --- plan ------------------------------------------------------------------


def test_plan_on_a_missing_project_lists_everything():
    pending = apply.plan(fake_gl(None), SPEC)
    assert pending[0].startswith("project +")
    assert any("board   + Dev Board" in x for x in pending)
    assert len(pending) == 5  # project, 2 labels, board, 1 issue


def test_plan_reports_nothing_when_the_board_already_matches():
    project = FakeProject(
        labels=["Doing", "Blocked"],
        boards=["Dev Board"],
        issues=[FakeIssue("one", labels=["Doing"])],
    )
    assert apply.plan(fake_gl(project), SPEC) == []


def test_plan_spots_a_changed_label():
    project = FakeProject(
        labels=["Doing", "Blocked"],
        boards=["Dev Board"],
        issues=[FakeIssue("one", labels=["Blocked"])],
    )
    pending = apply.plan(fake_gl(project), SPEC)
    assert pending == ["issue   ~ one: labels"]


def test_plan_never_proposes_deleting_what_the_yaml_omits():
    """Additive only — an issue not in the YAML is left alone, not removed."""
    project = FakeProject(
        labels=["Doing", "Blocked"],
        boards=["Dev Board"],
        issues=[FakeIssue("one", labels=["Doing"]), FakeIssue("not in yaml")],
    )
    assert apply.plan(fake_gl(project), SPEC) == []


# --- load ------------------------------------------------------------------


def test_load_rejects_a_spec_with_no_board(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text("project: grp/proj\n")
    with pytest.raises(SystemExit) as e:
        apply.load(str(f))
    assert "board" in str(e.value)


def test_load_defaults_columns_and_issues_to_empty(tmp_path):
    f = tmp_path / "b.yaml"
    f.write_text("project: grp/proj\nboard: B\n")
    spec = apply.load(str(f))
    assert spec["columns"] == [] and spec["issues"] == []
