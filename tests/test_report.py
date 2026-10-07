"""Tests for report.py — pure diffing over synthetic snapshot batches."""

from datetime import UTC, datetime

import pytest

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


def test_verifier_counts_windows_verdicts_by_author():
    history = [
        {
            "verdicts": [
                ["2026-09-12T00:00:00Z", "bob", "failed"],
                ["2026-09-13T00:00:00Z", "bob", "verified"],
                ["2026-09-01T00:00:00Z", "carol", "verified"],  # before start
            ]
        },
        {"verdicts": [["2026-09-14T00:00:00+00:00", "carol", "verified"]]},
    ]
    start = datetime(2026, 9, 7, tzinfo=UTC)
    end = datetime(2026, 9, 14, tzinfo=UTC)  # inclusive
    assert report.verifier_counts(history, start, end) == {"bob": 2, "carol": 1}
    assert report.verifier_counts([], start, end) == {}


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


def test_commit_counts_names_the_repo_when_git_log_fails_silently(monkeypatch):
    import subprocess
    import types

    def fail(*a, **k):
        return types.SimpleNamespace(returncode=128, stderr="", stdout="")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(OSError, match="git log failed in /r"):
        report.commit_counts("/r", 7)


# --- away: "since you were away" ----------------------------------------------

A0, A1 = "2026-10-05T10:00:00+00:00", "2026-10-06T10:00:00+00:00"
ANOW = datetime(2026, 10, 6, 12, tzinfo=UTC)


def arec(iid, ts, columns=("Doing",), due=None, board="B"):
    return {
        "ts": ts,
        "project": "g/p",
        "board": board,
        "iid": iid,
        "due_date": due,
        "columns": list(columns),
    }


def abatch(ts, *recs):
    return {r[0]: arec(r[0], ts, *r[1:]) for r in recs}


def test_away_counts_each_kind():
    before = abatch(A0, (1,), (2,), (3,), (4,), (5,), (6,))
    after = abatch(
        A1,
        (1, ["Review"]),  # moved
        (2, ["Done"]),  # moved, to Done; 3 is gone: closed
        (4, ["Doing"], "2026-10-04"),  # due before the window: not newly overdue
        (5, ["Doing"], "2026-10-06"),  # due today: not passed yet
        (6, ["Doing"], "2026-10-05"),  # passed while away
        (7,),  # new
    )
    got = report.away([before, after], A0, ANOW)
    assert got == {"moved": 2, "new": 1, "to Done": 1, "closed": 1, "newly overdue": 1}


def test_away_empty_and_nothing_to_compare():
    assert report.away([], A0, ANOW) == {}
    assert report.away([abatch(A0, (1,))], A0, ANOW) == {}  # latest is the baseline
    assert report.away([abatch(A1, (1,))], A0, ANOW) == {}  # nothing before


def test_away_is_per_board():
    before = {**abatch(A0, (1,)), 9: arec(9, A0, board="Other")}
    after = {**abatch(A1, (1, ["Done"])), 9: arec(9, A1, ["Done"], board="Other")}
    got = report.away([before, after], A0, ANOW, board="B")
    assert got == {"moved": 1, "to Done": 1}


def test_describe_omits_zeros_and_nothing_is_none():
    assert report.describe({}, ANOW) is None
    line = report.describe({"moved": 6, "new": 2}, ANOW)
    assert line == f"since {ANOW.astimezone().strftime('%a %H:%M')}: 6 moved · 2 new"


def test_seen_round_trip_and_corrupt(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert report.last_seen("g/p/B") is None
    report.mark_seen("g/p/B", ANOW)
    report.mark_seen("g/p/C", ANOW)
    assert report.last_seen("g/p/B") == ANOW
    seen = tmp_path / "gitboard" / "seen.json"
    seen.write_text("{nope")
    assert report.last_seen("g/p/B") is None
    report.mark_seen("g/p/B", ANOW)  # rewrites over the corrupt file
    assert report.last_seen("g/p/B") == ANOW
