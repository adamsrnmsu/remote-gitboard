"""Tests for board.py. No network — the GitLab API surface is faked.

board.py is a library module: no auth, no CLI. Auth lives in config.py and
error mapping in client.py, each with their own tests.
"""

import types
from datetime import UTC, datetime

from gitboard import board
from gitboard.apply import MARKER


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


class Mgr:
    def __init__(self, items):
        self.items = items

    def list(self, **_):
        return self.items


class FakeEvent:
    def __init__(self, ts, action, name):
        self.created_at, self.action = ts, action
        self.label = {"name": name} if name else None


class FakeNote:
    def __init__(self, ts, body, who="ana", system=False):
        self.created_at, self.body, self.system = ts, body, system
        self.author = {"username": who}


class HistIssue(FakeIssue):
    def __init__(self, iid, labels, state="opened", events=(), notes=(), **kw):
        super().__init__(iid, labels, **kw)
        self.state = state
        self.created_at = "2026-09-01T00:00:00Z"
        self.resourcelabelevents = Mgr(list(events))
        self.notes = Mgr(list(notes))


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
    out = board.as_markdown(
        FakeProject([FakeIssue(1, ["Doing"])]), FakeBoard([FakeList("Doing", 1)])
    )
    assert out.startswith("# grp/proj — Dev Board")
    assert "## Backlog (0)" in out
    assert "## Doing (1)" in out


def test_render_unassigned_and_assignee():
    issues = [FakeIssue(1, []), FakeIssue(2, [], assignee={"username": "ana"})]
    out = board.as_markdown(FakeProject(issues), FakeBoard([]))
    assert "#1 t — @unassigned" in out
    assert "#2 t — @ana" in out


def test_render_shows_due_date_and_extra_labels_but_not_the_column_label():
    issue = FakeIssue(1, ["Doing", "urgent"], due_date="2026-01-01")
    out = board.as_markdown(FakeProject([issue]), FakeBoard([FakeList("Doing", 1)]))
    line = next(x for x in out.splitlines() if x.startswith("- #1"))
    assert "due:2026-01-01" in line
    assert "`urgent`" in line
    assert "`Doing`" not in line, "the column's own label is redundant in its column"


def test_render_includes_the_issue_url():
    out = board.as_markdown(FakeProject([FakeIssue(7, [])]), FakeBoard([]))
    assert "http://gl/-/issues/7" in out


# --- rendering -------------------------------------------------------------


def test_issue_line_omits_the_column_its_own_label():
    line = board.issue_line(FakeIssue(1, ["Doing", "urgent"], title="x"), "Doing")
    text = line.plain
    assert "urgent" in text and "Doing" not in text


# --- urgency, totals, truncation -------------------------------------------


def test_overdue_is_strictly_before_today():
    assert board.is_overdue(FakeIssue(1, [], due_date="2026-01-01"), today="2026-06-01")
    assert not board.is_overdue(
        FakeIssue(1, [], due_date="2026-12-01"), today="2026-06-01"
    )


def test_due_today_is_not_overdue():
    """Off-by-one here would nag about everything due today."""
    assert not board.is_overdue(
        FakeIssue(1, [], due_date="2026-06-01"), today="2026-06-01"
    )


def test_no_due_date_is_never_overdue():
    assert not board.is_overdue(FakeIssue(1, []))


def test_columns_put_overdue_first():
    """A truncated column must show what you would have gone looking for."""
    issues = [
        FakeIssue(1, ["Doing"]),
        FakeIssue(2, ["Doing"], due_date="2000-01-01"),
        FakeIssue(3, ["Doing"]),
    ]
    cols = columns(issues, [FakeList("Doing", 1)])
    assert [i.iid for i in cols[1][1]][0] == 2


def test_columns_are_ordered_deterministically():
    """The API's own order is not stable; two calls must not differ."""
    issues = [FakeIssue(3, []), FakeIssue(1, []), FakeIssue(2, [])]
    first = [i.iid for i in columns(issues, [])[0][1]]
    second = [i.iid for i in columns(list(reversed(issues)), [])[0][1]]
    assert first == second == [3, 2, 1]


def test_summarise_does_not_double_count_multi_column_issues():
    """An issue in two columns is one issue, not two."""
    issue = FakeIssue(1, ["Doing", "Blocked"])
    cols = columns([issue], [FakeList("Doing", 1), FakeList("Blocked", 2)])
    assert board.summarise(cols)["issues"] == 1


def test_summarise_counts_unassigned_and_overdue():
    issues = [
        FakeIssue(1, [], assignee={"username": "ana"}),
        FakeIssue(2, []),
        FakeIssue(3, [], due_date="2000-01-01"),
    ]
    totals = board.summarise(columns(issues, []))
    assert (totals["issues"], totals["unassigned"], totals["overdue"]) == (3, 2, 1)


# --- snapshot_records ------------------------------------------------------


def test_snapshot_is_one_record_per_distinct_issue():
    """A two-column issue is one record with both columns, not two lines."""
    records = board.snapshot_records(
        FakeProject([FakeIssue(1, ["Doing", "Blocked"])]),
        FakeBoard([FakeList("Doing", 1), FakeList("Blocked", 2)]),
        ts="2026-08-21T00:00:00+00:00",
    )
    assert len(records) == 1
    assert records[0]["columns"] == ["Doing", "Blocked"]
    assert records[0]["iid"] == 1
    assert records[0]["ts"] == "2026-08-21T00:00:00+00:00"


def test_snapshot_records_assignee_and_backlog():
    records = board.snapshot_records(
        FakeProject([FakeIssue(1, [], assignee={"username": "alice"})]),
        FakeBoard([FakeList("Doing", 1)]),
        ts="t",
    )
    assert records[0]["assignee"] == "alice"
    assert records[0]["columns"] == ["Backlog"]


# --- offline: columns from a pulled spec -----------------------------------


SPEC = {
    "project": "grp/proj",
    "board": "Dev Board",
    "columns": [{"name": "Doing"}, {"name": "Review"}],
    "issues": [
        {"title": "both", "iid": 3, "labels": ["Doing", "Review"]},
        {"title": "loose", "iid": 4},
        {"title": "doing", "iid": 5, "labels": ["Doing"], "assignee": "bob"},
    ],
}


def test_columns_from_spec_matches_board_columns():
    live = columns(
        [
            FakeIssue(3, ["Doing", "Review"]),
            FakeIssue(4, []),
            FakeIssue(5, ["Doing"]),
        ],
        [FakeList("Doing", 1), FakeList("Review", 2)],
    )
    offline = board.columns_from_spec(SPEC, "http://gl")
    shape = lambda cols: [(n, [i.iid for i in issues]) for n, issues in cols]  # noqa: E731
    assert shape(offline) == shape(live)
    assert offline[0][0] == "Backlog"
    assert offline[1][1][0].web_url == "http://gl/grp/proj/-/issues/5"


def test_markdown_from_spec_handles_new_issues():
    spec = {**SPEC, "issues": [{"title": "a"}, {"title": "b", "labels": ["Doing"]}]}
    cols = board.columns_from_spec(spec, "http://gl")
    text = board.as_markdown(*board.spec_stand_ins(spec), columns=cols)
    assert "(new) a" in text and "(new) b" in text
    assert "None" not in text
    assert board.summarise(cols)["issues"] == 2


# --- age in column ---------------------------------------------------------


def test_markdown_appends_age_only_for_known_iids():
    """Append-only: the line up to the age is byte-identical to before."""
    issues = [FakeIssue(1, ["Doing"], due_date="2026-01-01"), FakeIssue(2, ["Doing"])]
    proj, brd = FakeProject(issues), FakeBoard([FakeList("Doing", 1)])
    plain = board.as_markdown(proj, brd)
    aged = board.as_markdown(proj, brd, ages={1: ("Doing", 3)})
    line = next(x for x in aged.splitlines() if x.startswith("- #1"))
    assert line.endswith(" age:3d")
    assert line[: -len(" age:3d")] in plain
    assert "age:" not in next(x for x in aged.splitlines() if x.startswith("- #2"))


def test_issue_line_and_board_view_show_age():
    line = board.issue_line(FakeIssue(1, ["Verify"]), "Verify", {1: ("Verify", 3)})
    assert line.plain.endswith("· Verify 3d")
    assert "·" not in board.issue_line(FakeIssue(1, ["Verify"]), "Verify").plain
    view, _ = board.board_view(
        FakeProject([FakeIssue(1, ["Verify"])]),
        FakeBoard([FakeList("Verify", 1)]),
        ages={1: ("Verify", 3)},
    )
    from io import StringIO

    from rich.console import Console

    from gitboard.log import THEME

    console = Console(width=120, file=StringIO(), theme=THEME)
    console.print(view)
    assert "Verify 3d" in console.file.getvalue()


# --- fetch_history ---------------------------------------------------------


class StateProject(FakeProject):
    """issues.list honours state=, like the API; records the kwargs."""

    def __init__(self, opened, closed):
        super().__init__(opened)
        self.calls = []
        by = {"opened": opened, "closed": closed}
        self.issues = types.SimpleNamespace(
            list=lambda **kw: (self.calls.append(kw), by[kw["state"]])[1]
        )


SINCE = datetime(2026, 9, 1, tzinfo=UTC)


def test_history_includes_closed_only_via_the_closed_list():
    opened = [HistIssue(1, ["Doing"])]
    closed = [HistIssue(2, [], state="closed"), HistIssue(1, ["Doing"])]
    proj = StateProject(opened, closed)
    history, columns = board.fetch_history(
        proj, FakeBoard([FakeList("Doing", 1)]), SINCE
    )
    assert columns == ["Doing"]
    assert [h["iid"] for h in history] == [1, 2]
    assert proj.calls[1]["updated_after"] == SINCE.isoformat()
    assert history[1]["state"] == "closed"
    assert history[1]["closed_at"] is None  # attribute missing: defensive read
    assert history[1]["tasks"] == [0, 0]


def test_history_reads_task_completion():
    i = HistIssue(1, [])
    i.task_completion_status = {"count": 4, "completed_count": 1}
    history, _ = board.fetch_history(StateProject([i], []), FakeBoard([]), SINCE)
    assert history[0]["tasks"] == [1, 4]


def test_history_keeps_only_column_label_events_and_skips_null_labels():
    events = [
        FakeEvent("2026-09-03T00:00:00Z", "add", "Doing"),
        FakeEvent("2026-09-02T00:00:00Z", "add", "bug"),
        FakeEvent("2026-09-04T00:00:00Z", "remove", None),
        FakeEvent("2026-09-01T00:00:00Z", "add", "Review"),
    ]
    proj = StateProject([HistIssue(1, ["Doing"], events=events)], [])
    lists = [FakeList("Doing", 1), FakeList("Review", 2)]
    history, _ = board.fetch_history(proj, FakeBoard(lists), SINCE)
    assert history[0]["transitions"] == [
        ["2026-09-01T00:00:00Z", "add", "Review"],
        ["2026-09-03T00:00:00Z", "add", "Doing"],
    ]


def test_history_parses_verdicts_after_the_marker_and_skips_system_notes():
    notes = [
        FakeNote("2026-09-05T00:00:00Z", f"{MARKER}\n\nVerified: works\nmore", "bob"),
        FakeNote("2026-09-04T00:00:00Z", "failed, see log", "cat"),
        FakeNote("2026-09-03T00:00:00Z", "Q: which branch?", "ana"),
        FakeNote("2026-09-02T00:00:00Z", "changed label", system=True),
    ]
    proj = StateProject([HistIssue(1, [], notes=notes)], [])
    history, _ = board.fetch_history(proj, FakeBoard([]), SINCE)
    assert history[0]["verdicts"] == [
        ["2026-09-04T00:00:00Z", "cat", "failed"],
        ["2026-09-05T00:00:00Z", "bob", "verified"],
    ]
    assert history[0]["notes"] == [
        ["2026-09-03T00:00:00Z", "ana", "Q: which branch?"],
        ["2026-09-04T00:00:00Z", "cat", "failed, see log"],
        ["2026-09-05T00:00:00Z", "bob", "Verified: works"],
    ]


def test_history_flattens_assignee_and_milestone():
    a = HistIssue(1, [], assignee={"username": "ana"})
    a.milestone = {"title": "M1"}
    b = HistIssue(2, [])
    b.milestone = None
    history, _ = board.fetch_history(StateProject([a, b], []), FakeBoard([]), SINCE)
    assert (history[0]["assignee"], history[0]["milestone"]) == ("ana", "M1")
    assert (history[1]["assignee"], history[1]["milestone"]) == (None, None)
    assert set(history[0]) == {
        "iid", "title", "state", "created_at", "closed_at", "updated_at",
        "assignee", "labels", "milestone", "due_date", "web_url", "tasks",
        "transitions", "verdicts", "notes",
    }  # fmt: skip


def test_board_view_marks_only_the_selected_card():
    from io import StringIO

    from rich.console import Console

    from gitboard.log import THEME

    proj = FakeProject([FakeIssue(1, ["Doing"], "one"), FakeIssue(2, ["Doing"], "two")])
    lists = FakeBoard([FakeList("Doing", 1)])

    def render(**kw):
        console = Console(width=120, file=StringIO(), theme=THEME)
        console.print(board.board_view(proj, lists, **kw)[0])
        return console.file.getvalue()

    assert "▶" not in render()
    marked = [x for x in render(selected=("Doing", 1)).splitlines() if "▶" in x]
    assert len(marked) == 1 and ("one" in marked[0]) != ("two" in marked[0])
