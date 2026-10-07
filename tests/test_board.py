"""Tests for board.py. No network — the GitLab API surface is faked.

board.py is a library module: no auth, no CLI. Auth lives in config.py and
error mapping in client.py, each with their own tests.
"""

import types
from datetime import UTC, datetime

import pytest

from gitboard import board
from gitboard.apply import MARKER


class FakeList:
    """A board list. `label=None` fakes an assignee/milestone list."""

    def __init__(self, name, position):
        self.position = position
        if name is not None:
            self.label = {"name": name}


class FakeIssue:
    def __init__(self, iid, labels, title="t", assignee=None, due_date=None, rp=None):
        self.iid, self.labels, self.title = iid, labels, title
        self.assignee, self.due_date = assignee, due_date
        self.relative_position = rp
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
    def __init__(
        self, iid, labels, state="opened", events=(), notes=(), links=(), **kw
    ):
        super().__init__(iid, labels, **kw)
        self.state = state
        self.created_at = "2026-09-01T00:00:00Z"
        self.resourcelabelevents = Mgr(list(events))
        self.notes = Mgr(list(notes))
        self.links = Mgr(list(links))


class FakeBoard:
    def __init__(self, lists, name="Dev Board"):
        self.name = name
        self.lists = type("L", (), {"list": lambda _s, **_: lists})()


class FakeProject:
    def __init__(self, issues, path="grp/proj"):
        self.path_with_namespace = path
        self.id = 7
        self.issues = type("I", (), {"list": lambda _s, **_: issues})()


def columns(issues, lists):
    return board.board_columns(FakeProject(issues), FakeBoard(lists))


# --- board_columns ---------------------------------------------------------


def test_lists_are_ordered_by_position_not_api_order():
    cols = columns([], [FakeList("Review", 3), FakeList("Doing", 1)])
    assert [n for n, _ in cols] == ["Backlog", "Doing", "Review"]


def test_issue_without_a_list_label_lands_in_backlog():
    """No labels at all, or only labels that are not a list."""
    cols = columns([FakeIssue(1, []), FakeIssue(2, ["bug"])], [FakeList("Doing", 1)])
    assert [i.iid for i in cols[0][1]] == [2, 1]
    assert cols[1][1] == []


def test_issue_in_two_lists_appears_in_both():
    """Mirrors the GitLab UI. Deliberately not tie-broken."""
    cols = columns(
        [FakeIssue(1, ["Doing", "Blocked"])],
        [FakeList("Doing", 1), FakeList("Blocked", 2)],
    )
    assert [i.iid for i in cols[1][1]] == [1]
    assert [i.iid for i in cols[2][1]] == [1]


def test_lists_without_a_label_are_skipped():
    """Assignee and milestone lists have no .label — they must not become columns."""
    cols = columns([FakeIssue(1, [])], [FakeList("Doing", 1), FakeList(None, 2)])
    assert [n for n, _ in cols] == ["Backlog", "Doing"]


def test_board_with_no_lists_puts_everything_in_backlog():
    cols = columns([FakeIssue(1, ["Doing"])], [])
    assert [n for n, _ in cols] == ["Backlog"]
    assert [i.iid for i in cols[0][1]] == [1]


# --- render ----------------------------------------------------------------


def test_render_header_counts_assignee_and_url():
    issues = [FakeIssue(1, []), FakeIssue(2, ["Doing"], assignee={"username": "ana"})]
    out = board.as_markdown(FakeProject(issues), FakeBoard([FakeList("Doing", 1)]))
    assert out.startswith("# grp/proj — Dev Board"), "header"
    assert "## Backlog (1)" in out and "## Doing (1)" in out, "counts"
    assert "#1 t — @unassigned" in out and "#2 t — @ana" in out, "assignee"
    assert "http://gl/-/issues/2" in out, "url"


def test_render_shows_due_date_and_extra_labels_but_not_the_column_label():
    issue = FakeIssue(1, ["Doing", "urgent"], due_date="2026-01-01")
    out = board.as_markdown(FakeProject([issue]), FakeBoard([FakeList("Doing", 1)]))
    line = next(x for x in out.splitlines() if x.startswith("- #1"))
    assert "due:2026-01-01" in line
    assert "`urgent`" in line
    assert "`Doing`" not in line, "the column's own label is redundant in its column"


# --- rendering -------------------------------------------------------------


# --- urgency, totals, truncation -------------------------------------------


@pytest.mark.parametrize(
    ("due", "overdue"),
    [("2026-01-01", True), ("2026-12-01", False), ("2026-06-01", False), (None, False)],
    ids=["past", "future", "today-not-overdue", "no-due-date"],
)
def test_overdue_is_strictly_before_today(due, overdue):
    """Off-by-one on today would nag about everything due today."""
    assert (
        board.is_overdue(FakeIssue(1, [], due_date=due), today="2026-06-01") is overdue
    )


def test_columns_are_ordered_deterministically():
    """The API's own order is not stable; two calls must not differ. Equal
    (null) board positions fall back to newest first."""
    issues = [FakeIssue(3, []), FakeIssue(1, []), FakeIssue(2, [])]
    first = [i.iid for i in columns(issues, [])[0][1]]
    second = [i.iid for i in columns(list(reversed(issues)), [])[0][1]]
    assert first == second == [3, 2, 1]


def test_columns_follow_board_order_after_overdue():
    """A truncated column must show what you would have gone looking for, so
    overdue is first even when the board order puts it last. Then GitLab's
    manual order (the one the lead sets through the YAML), nulls last; a due
    date no longer reorders anything but an overdue card."""
    issues = [
        FakeIssue(1, ["Doing"], rp=3),
        FakeIssue(2, ["Doing"], rp=1, due_date="2999-01-01"),
        FakeIssue(3, ["Doing"]),
        FakeIssue(4, ["Doing"], rp=9, due_date="2000-01-01"),
    ]
    cols = columns(issues, [FakeList("Doing", 1)])
    assert [i.iid for i in cols[1][1]] == [4, 2, 1, 3]


def test_summarise_counts_unassigned_and_overdue_once_per_issue():
    """An issue in two columns is one issue, not two."""
    issues = [
        FakeIssue(1, [], assignee={"username": "ana"}),
        FakeIssue(2, []),
        FakeIssue(3, ["Doing", "Blocked"], due_date="2000-01-01"),
    ]
    lists = [FakeList("Doing", 1), FakeList("Blocked", 2)]
    totals = board.summarise(columns(issues, lists))
    assert (totals["issues"], totals["unassigned"], totals["overdue"]) == (3, 2, 1)


# --- snapshot_records ------------------------------------------------------


def test_snapshot_is_one_record_per_distinct_issue():
    """A two-column issue is one record with both columns, not two lines."""
    records = board.snapshot_records(
        FakeProject(
            [
                FakeIssue(1, ["Doing", "Blocked"]),
                FakeIssue(2, [], assignee={"username": "alice"}),
            ]
        ),
        FakeBoard([FakeList("Doing", 1), FakeList("Blocked", 2)]),
        ts="2026-08-21T00:00:00+00:00",
    )
    assert len(records) == 2
    two = next(r for r in records if r["iid"] == 1)
    assert two["columns"] == ["Doing", "Blocked"]
    assert two["ts"] == "2026-08-21T00:00:00+00:00"
    loose = next(r for r in records if r["iid"] == 2)
    assert loose["assignee"] == "alice", "assignee"
    assert loose["columns"] == ["Backlog"], "backlog"


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
    """A pulled YAML lists issues in board order, so offline matches live."""
    live = columns(
        [
            FakeIssue(5, ["Doing"], rp=300),
            FakeIssue(3, ["Doing", "Review"], rp=100),
            FakeIssue(4, [], rp=200),
        ],
        [FakeList("Doing", 1), FakeList("Review", 2)],
    )
    offline = board.columns_from_spec(SPEC, "http://gl")
    shape = lambda cols: [(n, [i.iid for i in issues]) for n, issues in cols]  # noqa: E731
    assert shape(offline) == shape(live)
    assert offline[0][0] == "Backlog"
    assert offline[1][1][0].web_url == "http://gl/grp/proj/-/issues/3"


def test_columns_from_spec_use_yaml_order():
    """Offline, the YAML's list order is the board order; overdue still
    surfaces first, and each stand-in carries its milestone."""
    spec = {
        **SPEC,
        "issues": [
            {"title": "c", "iid": 1, "labels": ["Doing"]},
            {"title": "a", "labels": ["Doing"], "milestone": "Beta"},
            {"title": "b", "iid": 9, "labels": ["Doing"]},
            {"title": "late", "iid": 2, "labels": ["Doing"], "due_date": "2000-01-01"},
        ],
    }
    doing = board.columns_from_spec(spec, "http://gl")[1][1]
    assert [i.title for i in doing] == ["late", "c", "a", "b"]
    assert [i.relative_position for i in doing] == [3, 0, 1, 2]
    assert doing[2].milestone == {"title": "Beta"}
    assert doing[1].milestone is None


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
    own = board.issue_line(FakeIssue(1, ["Doing", "urgent"]), "Doing").plain
    assert "urgent" in own and "Doing" not in own, "column's own label omitted"
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


def test_history_exports_unanswered_questions():
    asked = [FakeNote("2026-09-03T00:00:00Z", "Q: which env?", "ana")]
    answered = asked + [FakeNote("2026-09-04T00:00:00Z", "prod", "bob")]
    proj = StateProject(
        [HistIssue(1, [], notes=asked), HistIssue(2, [], notes=answered)], []
    )
    history, _ = board.fetch_history(proj, FakeBoard([]), SINCE)
    by = {h["iid"]: h["questions"] for h in history}
    assert by[1] == [
        {"ts": "2026-09-03T00:00:00Z", "author": "ana", "text": "Q: which env?"}
    ]
    assert by[2] == []


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
    a.task_completion_status = {"count": 4, "completed_count": 1}
    history, _ = board.fetch_history(StateProject([a, b], []), FakeBoard([]), SINCE)
    assert (history[0]["tasks"], history[1]["tasks"]) == ([1, 4], [0, 0])
    assert (history[0]["assignee"], history[0]["milestone"]) == ("ana", "M1")
    assert (history[1]["assignee"], history[1]["milestone"]) == (None, None)
    assert set(history[0]) == {
        "iid", "title", "state", "created_at", "closed_at", "updated_at",
        "assignee", "labels", "milestone", "milestone_due", "priority",
        "due_date", "web_url", "tasks", "blocked_by", "transitions",
        "verdicts", "notes", "questions",
    }  # fmt: skip


def link(iid, project_id=7, full=None, kind="is_blocked_by", since="2026-09-20"):
    return types.SimpleNamespace(
        iid=iid,
        project_id=project_id,
        link_type=kind,
        issue_link_id=iid,
        link_created_at=since,
        references={"full": full or f"grp/proj#{iid}"},
    )


def test_history_carries_blocked_by_priority_and_milestone_due():
    """A blocker's state comes from the fetched issues: open and not in Done
    is opened; closed, in Done, or absent (every open issue was fetched) is
    closed; another project's card is unknown."""
    card = HistIssue(
        12,
        ["Doing", "priority::2", "priority::1"],
        links=[
            link(9),
            link(8),
            link(77),
            link(6),
            link(4, project_id=99, full="other/x#4"),
            link(5, kind="relates_to"),
        ],
    )
    card.milestone = {"title": "Beta", "due_date": "2026-11-01"}
    card.description = "Body\n\nBlocked by: #9, #3"
    opened = [
        card,
        HistIssue(9, ["Doing"]),
        HistIssue(6, ["Done"]),
        HistIssue(3, []),
        HistIssue(5, []),
    ]
    closed = [HistIssue(8, [], state="closed")]
    history, _ = board.fetch_history(StateProject(opened, closed), FakeBoard([]), SINCE)
    got = next(h for h in history if h["iid"] == 12)
    assert got["priority"] == 1
    assert (got["milestone"], got["milestone_due"]) == ("Beta", "2026-11-01")
    assert got["blocked_by"] == [
        {"ref": "6", "state": "closed", "since": "2026-09-20"},
        {"ref": "8", "state": "closed", "since": "2026-09-20"},
        {"ref": "9", "state": "opened", "since": "2026-09-20"},
        {"ref": "77", "state": "closed", "since": "2026-09-20"},
        {"ref": "other/x#4", "state": None, "since": "2026-09-20"},
        {"ref": "3", "state": "opened", "since": None},
    ]
    plain = next(h for h in history if h["iid"] == 9)
    assert (plain["priority"], plain["milestone_due"], plain["blocked_by"]) == (
        None,
        None,
        [],
    )


def test_history_takes_an_external_blockers_state_from_the_link():
    """The links API returns the linked issue's state: a closed card in
    another project is closed, not unknown. A footer ref stays unknown."""
    closed = link(4, project_id=99, full="other/x#4")
    closed.state = "closed"
    card = HistIssue(12, ["Doing"], links=[closed])
    card.description = "Blocked by: other/y#2"
    history, _ = board.fetch_history(StateProject([card], []), FakeBoard([]), SINCE)
    assert [(b["ref"], b["state"]) for b in history[0]["blocked_by"]] == [
        ("other/x#4", "closed"),
        ("other/y#2", None),
    ]


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


def test_board_view_limit_zero_shows_everything_and_limit_n_truncates():
    issues = [FakeIssue(n, ["Doing"]) for n in range(1, 4)]
    project, lists = FakeProject(issues), FakeBoard([FakeList("Doing", 1)])
    _, hidden = board.board_view(project, lists, limit=0)
    assert hidden == 0
    assert board.board_view(project, lists, limit=1)[1] == 2


def _filter_world():
    def card(iid, title, who=None, labels=(), ms=None):
        return types.SimpleNamespace(
            iid=iid,
            title=title,
            labels=list(labels),
            assignee={"username": who} if who else None,
            milestone={"title": ms} if ms else None,
        )

    return [
        (
            "Doing",
            [
                card(1, "Fix Login", "alice", ["bug", "Doing"], "v2"),
                card(2, "Rotate token", "bob", ["Doing"]),
            ],
        ),
        ("Review", [card(3, "Login docs", "alice", ["docs"], "v3")]),
        ("Done", []),
    ]


def _iids(cols):
    return [[i.iid for i in issues] for _, issues in cols]


def test_filter_columns_each_token_kind_keeps_every_column():
    cols = _filter_world()
    assert _iids(board.filter_columns(cols, "@ALICE")) == [[1], [3], []]
    assert _iids(board.filter_columns(cols, "~bug")) == [[1], [], []]
    assert _iids(board.filter_columns(cols, "%v3")) == [[], [3], []]
    assert _iids(board.filter_columns(cols, "login")) == [[1], [3], []]
    assert [n for n, _ in board.filter_columns(cols, "zzz")] == [
        "Doing",
        "Review",
        "Done",
    ]


def test_filter_columns_terms_are_anded_and_empty_query_is_a_noop():
    cols = _filter_world()
    assert _iids(board.filter_columns(cols, "@alice login ~bug")) == [[1], [], []]
    assert _iids(board.filter_columns(cols, "@bob login")) == [[], [], []]
    assert board.filter_columns(cols, "  ") is cols


def test_filter_columns_on_offline_spec_stand_ins():
    spec = {
        "project": "g/p",
        "board": "b",
        "columns": [{"name": "Doing"}],
        "issues": [
            {"iid": 1, "title": "one", "assignee": "alice", "labels": ["Doing"]},
            {"title": "two", "milestone": "v1"},
        ],
    }
    cols = board.columns_from_spec(spec, "http://gl")
    assert _iids(board.filter_columns(cols, "@alice")) == [[], [1]]
    assert _iids(board.filter_columns(cols, "%v1")) == [[None], []]
    assert _iids(board.filter_columns(cols, "@nobody")) == [[], []]
