"""Tests for stats.py — pure arithmetic over a fetch_history-shaped list."""

from datetime import UTC, datetime, timedelta
from email import message_from_string, policy

from gitboard import report, stats

COLUMNS = ["Doing", "Review", "Verify", "Done", "Failed"]
NOW = datetime(2026, 9, 16, 12, tzinfo=UTC)
END = NOW
START = NOW - timedelta(days=7)


def ts(day, hour=0):
    """A September 2026 timestamp, GitLab-style (Z suffix)."""
    return f"2026-09-{day:02d}T{hour:02d}:00:00.000Z"


def issue(iid, created=1, closed=None, labels=(), transitions=(), **kw):
    return {
        "iid": iid,
        "title": f"issue {iid}",
        "state": "closed" if closed else "opened",
        "created_at": ts(created),
        "closed_at": ts(closed) if closed else None,
        "updated_at": ts(closed or created),
        "assignee": kw.get("assignee"),
        "labels": list(labels),
        "milestone": None,
        "due_date": kw.get("due_date"),
        "web_url": f"http://x/{iid}",
        "transitions": [list(t) for t in transitions],
        "verdicts": [list(v) for v in kw.get("verdicts", ())],
        "notes": [list(n) for n in kw.get("notes", ())],
    }


def dt(day, hour=0):
    return datetime(2026, 9, day, hour, tzinfo=UTC)


# --- primitives ---------------------------------------------------------------


def test_parse_ts_handles_z_offset_and_none():
    assert stats.parse_ts(ts(1)) == dt(1)
    assert stats.parse_ts("2026-09-01T02:00:00+02:00") == dt(1)
    assert stats.parse_ts(None) is None


def test_scoped_takes_first_sorted_value():
    labels = ["story::Checkout", "epic::Payments", "epic::Auth", "stale"]
    assert stats.scoped(labels, "epic") == "Auth"
    assert stats.scopes(labels, "epic") == ["Auth", "Payments"]
    assert stats.scoped(labels, "type") is None


def test_dwell_open_interval():
    i = issue(1, labels=["Verify"], transitions=[(ts(3), "add", "Verify")])
    assert stats.dwell(i, "Verify") == [(dt(3), None)]


def test_dwell_closed_by_remove():
    i = issue(1, transitions=[(ts(3), "add", "Verify"), (ts(5), "remove", "Verify")])
    assert stats.dwell(i, "Verify") == [(dt(3), dt(5))]


def test_dwell_closed_by_issue_close():
    i = issue(1, closed=6, labels=["Verify"], transitions=[(ts(3), "add", "Verify")])
    assert stats.dwell(i, "Verify") == [(dt(3), dt(6))]


def test_dwell_reentered_is_two_intervals():
    i = issue(
        1,
        labels=["Verify"],
        transitions=[
            (ts(3), "add", "Verify"),
            (ts(4), "remove", "Verify"),
            (ts(6), "add", "Verify"),
        ],
    )
    assert stats.dwell(i, "Verify") == [(dt(3), dt(4)), (dt(6), None)]


def test_dwell_fallback_without_events_starts_at_created():
    assert stats.dwell(issue(1, created=2, labels=["Verify"]), "Verify") == [
        (dt(2), None)
    ]
    assert stats.dwell(issue(1, labels=["Doing"]), "Verify") == []


def test_dwell_remove_without_add_starts_at_created():
    i = issue(1, created=2, transitions=[(ts(5), "remove", "Verify")])
    assert stats.dwell(i, "Verify") == [(dt(2), dt(5))]


def test_done_at_prefers_close_over_done_label():
    i = issue(1, closed=9, labels=["Done"], transitions=[(ts(5), "add", "Done")])
    assert stats.done_at(i) == dt(9)
    i = issue(
        1,
        labels=["Done"],
        transitions=[(ts(5), "add", "Done"), (ts(8), "add", "Done")],
    )
    assert stats.done_at(i) == dt(8), "the last move into Done counts"
    assert stats.done_at(issue(1)) is None


def test_column_of_joins_and_defaults_to_backlog():
    assert stats.column_of(issue(1, labels=["Verify", "Doing", "x"]), COLUMNS) == (
        "Doing+Verify"
    )
    assert stats.column_of(issue(1, labels=["x"]), COLUMNS) == "Backlog"


# --- summarise ----------------------------------------------------------------


def history():
    return [
        # done this period (via Done label), 2 days in Verify, cycle 12 days
        issue(
            1,
            created=1,
            labels=["Done", "epic::Payments", "story::Checkout"],
            assignee="alice",
            transitions=[
                (ts(9), "add", "Verify"),
                (ts(11), "remove", "Verify"),
                (ts(13), "add", "Done"),
            ],
            verdicts=[(ts(13), "bob", "verified")],
        ),
        # closed this period, never verified, cycle 4 days
        issue(2, created=10, closed=14, labels=["epic::Payments"], assignee="bob"),
        # done last week (trend), 1 day in Verify
        issue(
            3,
            created=1,
            closed=5,
            labels=["epic::Auth"],
            assignee="alice",
            transitions=[(ts(3), "add", "Verify"), (ts(4), "remove", "Verify")],
            verdicts=[(ts(4), "root", "verified")],
        ),
        # in Verify 3 days, overdue, question waiting from root
        issue(
            4,
            created=8,
            labels=["Verify", "epic::Auth", "type::verify"],
            assignee="alice",
            due_date="2026-09-15",
            transitions=[(ts(13, 12), "add", "Verify")],
            notes=[(ts(14), "root", "Q: which env?")],
        ),
        # Doing 5 days (stuck), stale, two epics, question answered
        issue(
            5,
            created=8,
            labels=["Doing", "stale", "epic::A", "epic::B"],
            assignee="bob",
            transitions=[(ts(11), "add", "Doing")],
            notes=[(ts(12), "bob", "Q: ok?"), (ts(13), "root", "yes")],
        ),
        # failed this period, re-verify, Failed column
        issue(
            6,
            created=12,
            labels=["Failed", "re-verify"],
            assignee="bob",
            transitions=[(ts(12), "add", "Verify"), (ts(14), "remove", "Verify")],
            verdicts=[(ts(14), "bob", "failed")],
        ),
        issue(7, created=15),  # backlog, unassigned, opened this period
    ]


def test_summarise_counts():
    s = stats.summarise(history(), COLUMNS, START, END, NOW)
    assert s["period"]["days"] == 7
    assert s["open"]["total"] == 5
    assert s["open"]["by_column"] == {
        "Done": 1,
        "Verify": 1,
        "Doing": 1,
        "Failed": 1,
        "Backlog": 1,
    }
    assert s["open"]["by_epic"] == {"Payments": 1, "Auth": 1, "A": 1}
    assert s["open"]["by_type"] == {"verify": 1}
    assert s["open"]["unassigned"] == 1
    t = s["throughput"]
    assert (t["opened"], t["done"], t["closed"]) == (3, 2, 1)
    assert t["done_by"]["assignee"] == {"alice": 1, "bob": 1}
    assert t["done_by"]["epic"] == {"Payments": 2}
    assert t["cycle_days"] == {"median": 8.0, "mean": 8.0, "n": 2}


def test_summarise_verify_and_coverage():
    v = stats.summarise(history(), COLUMNS, START, END, NOW)["verify"]
    assert [(q["iid"], q["days"]) for q in v["queue"]] == [(4, 3.0)]
    assert v["oldest_days"] == 3.0
    assert (v["verified"], v["failed"]) == (1, 1)
    assert v["verifiers"] == {"bob": 2}
    assert v["verify_days"] == {"median": 2.0, "mean": 2.0, "n": 2}
    assert v["review_days"] == {"median": None, "mean": None, "n": 0}
    assert v["coverage"] == 0.5, "one of two done issues went through Verify"


def test_summarise_flow():
    f = stats.summarise(history(), COLUMNS, START, END, NOW)["flow"]
    assert f["overdue"] == 1
    assert (f["stale"], f["reverify"]) == (1, 1)
    assert f["wip"] == {"alice": 1, "bob": 1}
    assert f["multi_scope"] == [5]
    assert [(q["iid"], q["author"], q["text"]) for q in f["questions"]] == [
        (4, "root", "Q: which env?")
    ]


def test_stuck_reuses_report_thresholds(monkeypatch):
    s = stats.summarise(history(), COLUMNS, START, END, NOW)
    assert s["flow"]["stuck"] == [(5, "Doing", 5), (4, "Verify", 3)]
    monkeypatch.setattr(report, "STUCK", {"Doing": 10})
    s = stats.summarise(history(), COLUMNS, START, END, NOW)
    assert s["flow"]["stuck"] == []


def test_trend_uses_previous_period():
    tr = stats.summarise(history(), COLUMNS, START, END, NOW)["trend"]
    assert tr["done"] == (1, 2)
    assert tr["cycle_median"] == (4.0, 8.0)
    assert tr["verify_median"] == (1.0, 2.0)


def test_empty_history_is_none_not_zero():
    s = stats.summarise([], COLUMNS, START, END, NOW)
    assert s["throughput"]["cycle_days"]["median"] is None
    assert s["verify"]["oldest_days"] is None
    assert s["verify"]["coverage"] is None
    assert s["trend"]["done"] == (0, 0)
    assert s["flow"]["stuck"] == []


def test_questions_clear_on_reply_by_another_author_only():
    asked = issue(1, notes=[(ts(1), "root", "Q: why?"), (ts(2), "root", "still?")])
    assert [q["text"] for q in stats._questions(asked)] == ["Q: why?"]
    answered = issue(1, notes=[(ts(1), "root", "Q: why?"), (ts(2), "bob", "because")])
    assert stats._questions(answered) == []


# --- for_person ---------------------------------------------------------------


def test_for_person_queue_overdue_done_and_questions():
    h = history()
    s = stats.summarise(h, COLUMNS, START, END, NOW)
    p = stats.for_person(s, h, "alice", NOW)
    assert [q["iid"] for q in p["verify_queue"]] == [4]
    assert [o["iid"] for o in p["overdue"]] == [4]
    assert p["open_by_column"] == {"Done": 1, "Verify": 1}
    assert [d["iid"] for d in p["done"]] == [1]
    assert (p["verified"], p["failed"]) == (0, 0)
    assert [q["iid"] for q in p["questions"]] == [4]
    b = stats.for_person(s, h, "bob", NOW)
    assert (b["verified"], b["failed"]) == (1, 1)
    assert b["questions"] == [], "bob's own question is not waiting on bob"


# --- rendering ----------------------------------------------------------------


def test_render_team_md_smoke():
    md = stats.render_team_md(stats.summarise(history(), COLUMNS, START, END, NOW))
    assert "**Open 5**" in md
    assert "done 2 ▲ (prev 1)" in md
    assert "| cycle (created → done) | 8.0 | 8.0 | 2 |" in md
    assert "| in Review | – | – | 0 |" in md
    assert "coverage 50%" in md
    assert "| 4 | issue 4 | alice | 3.0 |" in md
    assert "| 5 | Doing | 5 |" in md
    assert "Q: which env?" in md


def test_render_person_md_own_section_then_team():
    h = history()
    s = stats.summarise(h, COLUMNS, START, END, NOW)
    md = stats.render_person_md(stats.for_person(s, h, "alice", NOW), s, "alice")
    assert md.startswith("# alice\n")
    assert "Done 1 · verified 0 · failed 0" in md
    assert "| 4 | issue 4 | 2026-09-15 |" in md
    own, _, team = md.partition("\n---\n")
    assert "# Team" not in own and "# Team" in team


def test_eml_headers_and_body():
    raw = stats.eml("a@x.dev", "[g/p] week — alice", "# alice\n\nDone ✓\n", now=NOW)
    msg = message_from_string(raw, policy=policy.default)
    assert msg["To"] == "a@x.dev"
    assert msg["Subject"] == "[g/p] week — alice"
    assert msg["Date"] == "Wed, 16 Sep 2026 12:00:00 +0000"
    assert msg["From"] is None
    assert msg.get_content_type() == "text/plain"
    assert msg.get_payload(decode=True).decode("utf-8") == "# alice\n\nDone ✓\n"
    assert "From: me@x.dev" in stats.eml("a@x.dev", "s", "b", sender="me@x.dev")


def test_slug():
    assert stats.slug("grp/sub/proj") == "grp-sub-proj"


def test_days_never_negative():

    now = datetime(2026, 9, 17, tzinfo=UTC)
    assert stats._days(now + timedelta(seconds=30), now) == 0.0
