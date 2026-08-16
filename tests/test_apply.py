"""Tests for apply.py. No network — the GitLab API surface is faked.

The two bugs these pin both cost a real debugging round: YAML parses an
unquoted date into a date object (not JSON-serialisable), and GitLab strips
the trailing newline that YAML's `|` preserves (phantom diff on every run).
"""

import datetime
import types

import pytest

import apply
import client


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
