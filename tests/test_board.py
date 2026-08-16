"""Tests for board.py. No network — the GitLab API surface is faked.

board.py defers `import gitlab` into main(), so importing it here needs no deps.
"""

import pytest

import board


class FakeList:
    """A board list. `label=None` fakes an assignee/milestone list."""

    def __init__(self, name, position):
        self.position = position
        if name is not None:
            self.label = {"name": name}


class FakeIssue:
    def __init__(self, iid, labels, title="t", assignee=None, due_date=None):
        self.iid, self.labels, self.title = iid, labels, title
        self.assignee, self.due_date = assignee, due_date
        self.web_url = f"http://gl/-/issues/{iid}"


class FakeBoard:
    def __init__(self, lists, name="Dev Board"):
        self.name = name
        self.lists = type("L", (), {"list": lambda _s, **_: lists})()


class FakeProject:
    def __init__(self, issues, path="grp/proj"):
        self.path_with_namespace = path
        self.issues = type("I", (), {"list": lambda _s, **_: issues})()


def columns(issues, lists):
    return board.board_columns(FakeProject(issues), FakeBoard(lists))


# --- board_columns ---------------------------------------------------------


def test_lists_are_ordered_by_position_not_api_order():
    cols = columns([], [FakeList("Review", 3), FakeList("Doing", 1)])
    assert [n for n, _ in cols] == ["Backlog", "Doing", "Review"]


def test_unlabelled_issue_lands_in_backlog():
    cols = columns([FakeIssue(1, [])], [FakeList("Doing", 1)])
    assert [i.iid for i in cols[0][1]] == [1]


def test_issue_in_two_lists_appears_in_both():
    """Mirrors the GitLab UI. Deliberately not tie-broken."""
    cols = columns(
        [FakeIssue(1, ["Doing", "Blocked"])],
        [FakeList("Doing", 1), FakeList("Blocked", 2)],
    )
    assert [i.iid for i in cols[1][1]] == [1]
    assert [i.iid for i in cols[2][1]] == [1]


def test_issue_with_an_unrelated_label_still_counts_as_backlog():
    cols = columns([FakeIssue(1, ["bug"])], [FakeList("Doing", 1)])
    assert [i.iid for i in cols[0][1]] == [1]
    assert cols[1][1] == []


def test_lists_without_a_label_are_skipped():
    """Assignee and milestone lists have no .label — they must not become columns."""
    cols = columns([FakeIssue(1, [])], [FakeList("Doing", 1), FakeList(None, 2)])
    assert [n for n, _ in cols] == ["Backlog", "Doing"]


def test_board_with_no_lists_puts_everything_in_backlog():
    cols = columns([FakeIssue(1, ["Doing"])], [])
    assert [n for n, _ in cols] == ["Backlog"]
    assert [i.iid for i in cols[0][1]] == [1]


# --- render ----------------------------------------------------------------


def test_render_header_and_counts():
    out = board.render(
        FakeProject([FakeIssue(1, ["Doing"])]), FakeBoard([FakeList("Doing", 1)])
    )
    assert out.startswith("# grp/proj — Dev Board")
    assert "## Backlog (0)" in out
    assert "## Doing (1)" in out


def test_render_unassigned_and_assignee():
    issues = [FakeIssue(1, []), FakeIssue(2, [], assignee={"username": "ana"})]
    out = board.render(FakeProject(issues), FakeBoard([]))
    assert "#1 t — @unassigned" in out
    assert "#2 t — @ana" in out


def test_render_shows_due_date_and_extra_labels_but_not_the_column_label():
    issue = FakeIssue(1, ["Doing", "urgent"], due_date="2026-01-01")
    out = board.render(FakeProject([issue]), FakeBoard([FakeList("Doing", 1)]))
    line = next(x for x in out.splitlines() if x.startswith("- #1"))
    assert "due:2026-01-01" in line
    assert "`urgent`" in line
    assert "`Doing`" not in line, "the column's own label is redundant in its column"


def test_render_includes_the_issue_url():
    out = board.render(FakeProject([FakeIssue(7, [])]), FakeBoard([]))
    assert "http://gl/-/issues/7" in out


# --- token -----------------------------------------------------------------


def test_env_token_wins_over_keychain(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "from-env")
    monkeypatch.setattr(
        board.subprocess, "run", lambda *a, **k: pytest.fail("keychain was consulted")
    )
    assert board.token() == "from-env"


def test_keychain_is_the_fallback(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(
        board.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "from-keychain\n"})(),
    )
    assert board.token() == "from-keychain"


def test_missing_token_exits_rather_than_returning_empty(monkeypatch):
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(
        board.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 1, "stdout": ""})(),
    )
    with pytest.raises(SystemExit):
        board.token()
