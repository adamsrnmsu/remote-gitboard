"""Tests for report.py — pure diffing over synthetic snapshot batches."""

from gitboard import report


def rec(iid, columns=("Doing",), assignee=None):
    return {
        "iid": iid,
        "title": f"issue {iid}",
        "assignee": assignee,
        "columns": list(columns),
    }


def test_diff_finds_new_closed_moved_and_still():
    first = {1: rec(1), 2: rec(2, ["Doing"]), 3: rec(3)}
    last = {2: rec(2, ["Review"]), 3: rec(3), 4: rec(4)}
    d = report.diff([first, last])
    assert [r["iid"] for r in d["new"]] == [4]
    assert [r["iid"] for r in d["closed"]] == [1]
    assert [(a["iid"], b["columns"]) for a, b in d["moved"]] == [(2, ["Review"])]
    assert [r["iid"] for r in d["unchanged"]] == [3]


def test_middle_batches_do_not_matter():
    """The window is first-vs-last; a bounce out and back is unchanged."""
    a, b = {1: rec(1, ["Doing"])}, {1: rec(1, ["Blocked"])}
    assert report.diff([a, b, a])["unchanged"][0]["iid"] == 1


def test_by_assignee_attributes_to_latest_sighting():
    d = report.diff(
        [
            {1: rec(1, ["Doing"], "alice"), 2: rec(2, assignee="bob")},
            {1: rec(1, ["Review"], "alice"), 3: rec(3)},
        ]
    )
    tally = report.by_assignee(d)
    assert tally["alice"]["moved"] == 1
    assert tally["bob"]["closed"] == 1
    assert tally["unassigned"]["new"] == 1


def test_match_author_by_name_or_email_local_part():
    authors = {("Ryan Adams", "ryan@x.dev"): 3, ("alice", "a@y.dev"): 1}
    assert report.match_author("ryan", authors) == ("Ryan Adams", "ryan@x.dev")
    assert report.match_author("Alice", authors) == ("alice", "a@y.dev")
    assert report.match_author("nobody", authors) is None
