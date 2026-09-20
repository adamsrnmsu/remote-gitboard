"""Tests for estimate.py — pure arithmetic over a fetch_history-shaped list."""

from datetime import date

import pytest
from test_stats import COLUMNS, NOW, issue, ts

from gitboard import estimate
from gitboard.apply import SpecError


def done(iid, who, days, kind=None, start=1):
    """A card `who` finished `days` after it entered Doing on day `start`."""
    return issue(
        iid,
        created=1,
        closed=start + days,
        assignee=who,
        labels=[f"type::{kind}"] if kind else [],
        transitions=[(ts(start), "add", "Doing")],
    )


def test_config_defaults_overrides_and_rejections():
    assert estimate.config({}) == estimate.DEFAULTS
    cfg = estimate.config({"estimates": {"suggest_due": False, "method": "median"}})
    assert (cfg["suggest_due"], cfg["method"], cfg["min_samples"]) == (
        False,
        "median",
        5,
    )
    for bad in ("yes", {"method": "p99"}, {"min_samples": 0}, {"min_samples": "3"}):
        with pytest.raises(SpecError):
            estimate.config({"estimates": bad})


def test_active_days_start_at_the_first_column_add_not_creation():
    i = issue(1, created=1, closed=9, assignee="ana",
              transitions=[(ts(6), "add", "Doing"), (ts(8), "add", "Verify")])  # fmt: skip
    assert estimate.samples([i]) == [("ana", None, 3.0)]
    assert estimate.samples([issue(2, created=2, closed=4)]) == [(None, None, 2.0)]
    assert estimate.samples([issue(3)]) == []  # not finished: not a sample


def test_nearest_rank_percentiles_round_up_to_whole_days():
    pool = [("ana", None, d) for d in (1, 2, 3, 4, 10.2)]
    assert estimate.estimate("ana", [], pool, "p85")["days"] == 11
    assert estimate.estimate("ana", [], pool, "median")["days"] == 3
    assert estimate.estimate("ana", [], [("ana", None, 0.0)] * 5)["days"] == 1


def test_ladder_takes_the_narrowest_bucket_with_enough_samples():
    pool = (
        [("ana", "bug", 2.0)] * 2
        + [("ana", "task", 4.0)] * 3
        + [("bob", "bug", 6.0)] * 3
        + [("bob", "task", 8.0)] * 9
    )
    bug = ["type::bug"]
    assert estimate.estimate("ana", bug, pool, min_samples=2)["basis"] == (
        "p85 of 2 cards: ana, type::bug"
    )
    assert estimate.estimate("ana", bug, pool, min_samples=5)["basis"] == (
        "p85 of 5 cards: ana"
    )
    assert estimate.estimate("cat", bug, pool, min_samples=5)["basis"] == (
        "p85 of 5 cards: team, type::bug"
    )
    assert estimate.estimate("cat", [], pool, min_samples=5)["basis"] == (
        "p85 of 17 cards: team"
    )
    assert estimate.estimate("cat", [], pool, min_samples=18) is None


def _spec(**estimates):
    spec = {
        "issues": [
            {"title": "open", "assignee": "ana", "labels": ["Doing"]},
            {"title": "dated", "assignee": "ana", "due_date": "2026-10-01"},
            {"title": "nobody"},
            {"title": "waiting", "assignee": "ana", "labels": ["Verify"]},
        ]
    }
    if estimates:
        spec["estimates"] = estimates
    return spec


HISTORY = [done(n, "ana", 3) for n in range(1, 6)]


def test_suggest_dates_only_assigned_undated_cards_outside_verify_done_failed():
    spec = _spec()
    out = estimate.suggest(spec, HISTORY, date(2026, 9, 20))
    assert out["changed"] and [r["title"] for r in out["rows"]] == ["open"]
    assert spec["issues"][0]["due_date"] == "2026-09-23"
    assert spec["issues"][1]["due_date"] == "2026-10-01"
    assert "due_date" not in spec["issues"][2] and "due_date" not in spec["issues"][3]


def test_suggest_due_false_reports_but_writes_nothing():
    spec = _spec(suggest_due=False)
    out = estimate.suggest(spec, HISTORY, date(2026, 9, 20))
    assert not out["changed"] and out["rows"][0]["due"] == "2026-09-23"
    assert "due_date" not in spec["issues"][0]


def test_suggest_without_enough_history_is_empty():
    assert estimate.suggest(_spec(), HISTORY[:4], date(2026, 9, 20)) == {
        "rows": [],
        "changed": False,
    }


def _open(iid, due, entered=None, **kw):
    return issue(
        iid,
        assignee="ana",
        due_date=due,
        labels=["Doing"] if entered else [],
        transitions=[(ts(entered), "add", "Doing")] if entered else [],
        **kw,
    )


def test_tight_flags_a_due_date_before_the_expected_finish():
    # NOW is 2026-09-16; ana's cards take 3 days
    history = HISTORY + [
        _open(10, "2026-09-17", entered=16),  # 16 + 3 = 19 > 17: tight
        _open(11, "2026-09-19", entered=16),  # 19 <= 19: fine
        _open(12, "2026-09-15", entered=10),  # already overdue: not ours
        _open(13, "2026-09-18"),  # Backlog: today + 3 = 19 > 18: tight
        _open(14, "2026-09-16", entered=1),  # long started: expected is today
    ]
    rows = estimate.tight(history, COLUMNS, NOW, estimate.DEFAULTS)
    assert [(r["iid"], r["due"], r["expected"]) for r in rows] == [
        (10, "2026-09-17", "2026-09-19"),
        (13, "2026-09-18", "2026-09-19"),
    ]
    assert rows[0]["basis"] == "p85 of 5 cards: ana" and rows[0]["assignee"] == "ana"
