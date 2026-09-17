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


# --- age in column ---------------------------------------------------------

from datetime import UTC, datetime  # noqa: E402


def batch(ts, *recs):
    return {r["iid"]: {**r, "ts": ts} for r in recs}


T = [f"2026-09-{d:02d}T00:00:00+00:00" for d in range(10, 16)]
NOW = datetime(2026, 9, 15, 12, tzinfo=UTC)


def test_column_ages_streak_starts_at_last_change():
    batches = [
        batch(T[0], rec(1, ["Doing"]), rec(2, ["Verify"])),
        batch(T[1], rec(1, ["Doing"]), rec(2, ["Doing"])),
        batch(T[2], rec(1, ["Doing"]), rec(2, ["Verify"])),
        batch(T[3], rec(1, ["Doing"]), rec(2, ["Verify"])),
    ]
    ages = report.column_ages(batches)
    assert ages[1] == ("Doing", T[0]), "never moved: streak is the whole log"
    assert ages[2] == ("Verify", T[2]), "a bounce out and back restarts the streak"


def test_column_ages_drops_closed_and_joins_two_columns():
    batches = [
        batch(T[0], rec(1, ["Doing"]), rec(2, ["Doing", "Blocked"])),
        batch(T[1], rec(2, ["Doing", "Blocked"])),
    ]
    ages = report.column_ages(batches)
    assert 1 not in ages
    assert ages[2] == ("Doing+Blocked", T[0])
    assert report.column_ages([]) == {}


def test_age_days_and_stuck_thresholds():
    ages = {1: ("Verify", T[3]), 2: ("Doing", T[4]), 3: ("Doing+Verify", T[0])}
    assert report.age_days(ages, NOW) == {
        1: ("Verify", 2),
        2: ("Doing", 1),
        3: ("Doing+Verify", 5),
    }
    assert report.stuck(ages, now=NOW) == [(3, "Doing+Verify", 5), (1, "Verify", 2)]
    assert report.stuck(ages, {"Doing": 1}, NOW) == [
        (3, "Doing+Verify", 5),
        (2, "Doing", 1),
    ]
    assert report.stuck(ages, {}, NOW) == []


def test_since_keeps_batches_at_or_after_ts():
    batches = [batch(T[0], rec(1)), batch(T[2], rec(1)), batch(T[4], rec(1))]
    kept = report.since(batches, T[2])
    assert [next(iter(b.values()))["ts"] for b in kept] == [T[2], T[4]]
    assert report.since(batches, "2099-01-01") == []
