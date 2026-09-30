"""Tests for stats.py — pure arithmetic over a fetch_history-shaped list."""

from datetime import UTC, datetime, timedelta
from email import message_from_string, policy

import pytest

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


@pytest.mark.parametrize(
    ("i", "want"),
    [
        (
            issue(1, labels=["Verify"], transitions=[(ts(3), "add", "Verify")]),
            [(dt(3), None)],
        ),
        (
            issue(
                1, transitions=[(ts(3), "add", "Verify"), (ts(5), "remove", "Verify")]
            ),
            [(dt(3), dt(5))],
        ),
        (
            issue(
                1, closed=6, labels=["Verify"], transitions=[(ts(3), "add", "Verify")]
            ),
            [(dt(3), dt(6))],
        ),
        (
            issue(
                1,
                labels=["Verify"],
                transitions=[
                    (ts(3), "add", "Verify"),
                    (ts(4), "remove", "Verify"),
                    (ts(6), "add", "Verify"),
                ],
            ),
            [(dt(3), dt(4)), (dt(6), None)],
        ),
        (issue(1, created=2, labels=["Verify"]), [(dt(2), None)]),
        (issue(1, labels=["Doing"]), []),
        (
            issue(1, created=2, transitions=[(ts(5), "remove", "Verify")]),
            [(dt(2), dt(5))],
        ),
    ],
    ids=[
        "open",
        "closed-by-remove",
        "closed-by-issue-close",
        "reentered",
        "no-events-from-created",
        "no-events-other-label",
        "remove-without-add",
    ],
)
def test_dwell(i, want):
    assert stats.dwell(i, "Verify") == want


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
    assert tr["verify_queue"] == (1, 1), "issue 1 was in Verify at start; 4 is now"


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


def test_eml_html_is_multipart_alternative():
    raw = stats.eml("a@x.dev", "s", "plain\n", now=NOW, html="<p>hi</p>")
    msg = message_from_string(raw, policy=policy.default)
    assert msg.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in msg.iter_parts()] == [
        "text/plain",
        "text/html",
    ]
    assert msg.get_body(("plain",)).get_content() == "plain\n"
    assert "<p>hi</p>" in msg.get_body(("html",)).get_content()
    plain = message_from_string(stats.eml("a@x.dev", "s", "b"), policy=policy.default)
    assert not plain.is_multipart()


def test_slug():
    assert stats.slug("grp/sub/proj") == "grp-sub-proj"


def test_days_never_negative():

    now = datetime(2026, 9, 17, tzinfo=UTC)
    assert stats._days(now + timedelta(seconds=30), now) == 0.0


# --- daily_series -------------------------------------------------------------


def series_by_date(h):
    return {r["date"]: r for r in stats.daily_series(h, COLUMNS, START, END)}


def test_daily_series_open_from_created_day_until_done_day():
    dates = [r["date"] for r in stats.daily_series([], COLUMNS, START, END)]
    assert dates[0] == "2026-09-09" and dates[-1] == "2026-09-16"
    assert len(dates) == 8, "one row per day, inclusive"
    i = issue(1, created=12, closed=14)
    i["closed_at"] = ts(14, 6)  # mid-day, as real timestamps are
    rows = stats.daily_series([i], COLUMNS, START, END)
    assert [r["open"] for r in rows] == [0, 0, 1, 1, 1, 0, 0, 0]
    assert [r["done_cum"] for r in rows] == [0, 0, 0, 0, 0, 1, 1, 1]
    i = issue(1)
    i["created_at"] = None
    assert series_by_date([i])["2026-09-09"]["open"] == 1, "no created: from start"


def test_daily_series_verify_from_interval_start():
    s = series_by_date(history())
    # issue 4 enters Verify Sep 13 12:00 and is still there; 6 was in 12..14
    assert s["2026-09-12"]["verify"] == 1
    assert s["2026-09-13"]["verify"] == 1
    assert s["2026-09-14"]["verify"] == 1
    assert s["2026-09-10"]["verify"] == 0


def test_daily_series_done_cum_monotonic_and_matches_summary():
    rows = stats.daily_series(history(), COLUMNS, START, END)
    cum = [r["done_cum"] for r in rows]
    assert cum == sorted(cum)
    done = stats.summarise(history(), COLUMNS, START, END, NOW)["throughput"]["done"]
    assert cum[-1] == done == 2


# --- momentum -----------------------------------------------------------------


def m(done, queue=None):
    tr = {"done": done}
    if queue:
        tr["verify_queue"] = queue
    return stats.momentum({"trend": tr})


@pytest.mark.parametrize(
    ("done", "queue", "want"),
    [
        ((6, 9), None, "Done 9, up from 6."),
        ((9, 6), None, "Done 6, down from 9."),
        ((6, 6), None, "Done 6, flat."),
        ((None, 3), None, "Done 3."),
        ((6, 9), (5, 3), "Done 9, up from 6. Verify queue 3 (was 5) — shrinking!"),
        ((6, 9), (3, 5), "Done 9, up from 6. Verify queue 5 (was 3) — growing."),
        ((9, 6), (5, 3), "Done 6, down from 9. Verify queue 3 (was 5) — shrinking."),
        ((6, 6), (3, 3), "Done 6, flat. Verify queue 3 (was 3) — flat."),
        ((None, 3), (5, 3), "Done 3. Verify queue 3 (was 5) — shrinking."),  # no "!"
    ],
)
def test_momentum(done, queue, want):
    assert m(done, queue) == want


# --- three_moves --------------------------------------------------------------


def test_three_moves_order_and_shape():
    h = history()
    s = stats.summarise(h, COLUMNS, START, END, NOW)
    moves = stats.three_moves(stats.for_person(s, h, "alice", NOW))
    # #4 is in Verify, overdue and carries a question: one move, not three
    assert [(x["verb"], x["iid"], x["age"]) for x in moves] == [
        ("Verify", 4, "3.0 d in Verify"),
    ]
    assert all(x["url"] == "http://x/4" and x["title"] == "issue 4" for x in moves)


def test_three_moves_dedupes_by_card_and_keeps_the_first_verb():
    person = {
        "verify_queue": [{"iid": 1, "title": "a", "days": 2.0, "url": "u1"}],
        "overdue": [
            {"iid": 1, "title": "a", "due": "2026-09-01", "url": "u1"},
            {"iid": 2, "title": "b", "due": "2026-09-02", "url": "u2"},
        ],
        "questions": [
            {"iid": 3, "title": "c", "days": 1.0, "author": "root", "url": "u3"}
        ],
    }
    assert [(m["verb"], m["iid"]) for m in stats.three_moves(person)] == [
        ("Verify", 1),
        ("Finish", 2),
        ("Answer", 3),
    ]


def test_three_moves_caps_and_fills_from_queue():
    q = [
        {"iid": n, "title": f"t{n}", "days": float(9 - n), "url": f"u{n}"}
        for n in range(1, 6)
    ]
    moves = stats.three_moves({"verify_queue": q, "overdue": [], "questions": []})
    assert [(x["verb"], x["iid"]) for x in moves] == [
        ("Verify", 1),
        ("Verify", 2),
        ("Verify", 3),
    ]
    assert stats.three_moves({"verify_queue": [], "overdue": [], "questions": []}) == []


# --- url on items -------------------------------------------------------------


def test_items_carry_url():
    h = history()
    s = stats.summarise(h, COLUMNS, START, END, NOW)
    assert s["verify"]["queue"][0]["url"] == "http://x/4"
    assert s["flow"]["overdue"] == 1
    assert [(o["iid"], o["due"], o["url"]) for o in s["flow"]["overdue_items"]] == [
        (4, "2026-09-15", "http://x/4")
    ]
    assert s["flow"]["questions"][0]["url"] == "http://x/4"
    p = stats.for_person(s, h, "alice", NOW)
    for key in ("verify_queue", "overdue", "done", "questions"):
        assert all(x["url"].startswith("http://x/") for x in p[key]), key


# --- over time ----------------------------------------------------------------

ROW_KEYS = {
    "blocked_stale",
    "blocked_unmarked",
    "priority_inversion",
    "date_inversion",
    "unowned_blocker",
    "no_milestone",
    "weak",
    "tight",
    "late_milestones",
    "ts",
    "project",
    "board",
    "period_start",
    "period_end",
    "days",
    "open",
    "done",
    "closed",
    "verify_queue",
    "verify_median",
    "review_median",
    "cycle_median",
    "coverage",
    "overdue",
    "stuck",
    "by_epic",
    "by_milestone",
    "by_assignee",
}


def row(project="g/p", board="dev", end="2026-09-16T12:00:00+00:00", **kw):
    return {"project": project, "board": board, "period_end": end, **kw}


def test_stat_row_keys_and_values():
    s = stats.summarise(history(), COLUMNS, START, END, NOW)
    r = stats.stat_row(s, "g/p", "dev", "2026-09-16T12:00:00+00:00")
    assert set(r) == ROW_KEYS
    assert r["period_end"] == END.isoformat() and r["days"] == 7
    assert (r["open"], r["done"], r["closed"]) == (5, 2, 1)
    assert (r["verify_queue"], r["overdue"], r["stuck"]) == (1, 1, 2)
    assert (r["verify_median"], r["review_median"], r["cycle_median"]) == (
        2.0,
        None,
        8.0,
    )
    assert r["coverage"] == 0.5
    assert r["by_epic"] == {"Payments": 1, "Auth": 1, "A": 1}
    assert r["by_assignee"] == {"alice": 2, "bob": 2}
    empty = stats.stat_row({}, "g/p", None, "t")
    assert set(empty) == ROW_KEYS and empty["open"] == 0 and empty["coverage"] is None


def test_append_row_dedupes_per_week_and_later_wins(tmp_path):
    path = tmp_path / "reports" / "stats.jsonl"
    stats.append_row(path, row(done=1))
    stats.append_row(path, row(end="2026-09-16T18:00:00+00:00", done=2))
    stats.append_row(path, row(board="ops", done=3))
    stats.append_row(path, row(end="2026-09-09T12:00:00+00:00", done=4))
    rows = stats.load_rows(path)
    assert [r["done"] for r in rows] == [2, 3, 4]
    assert path.read_text().count("\n") == 3
    assert stats.load_rows(tmp_path / "nope.jsonl") == [], "missing file"


def test_weekly_sorts_caps_and_filters():
    rows = [
        row(end=f"2026-09-{d:02d}T00:00:00+00:00", done=d) for d in (16, 2, 9, 23)
    ] + [row(project="other", end="2026-09-30", done=99), row(board="ops", done=98)]
    assert [r["done"] for r in stats.weekly(rows, "g/p")] == [2, 9, 16, 98, 23]
    assert [r["done"] for r in stats.weekly(rows, "g/p", weeks=2)] == [98, 23]
    assert [r["done"] for r in stats.weekly(rows, "g/p", board="ops")] == [98]
    assert stats.weekly(rows, "nobody") == []


def test_render_weekly_md_dashes_and_percent():
    assert stats.render_weekly_md([]) == "_none_"
    md = stats.render_weekly_md(
        [row(done=2, open=5, verify_queue=1, coverage=0.5, overdue=1, stuck=2)]
    )
    assert md.startswith(
        "| week | done | open | verify q | verify d | review d | cycle d "
        "| coverage | overdue | stuck |\n"
    )
    assert "| 2026-09-16 | 2 | 5 | 1 | – | – | – | 50% | 1 | 2 |" in md


def test_render_team_md_weekly_is_opt_in():
    s = stats.summarise(history(), COLUMNS, START, END, NOW)
    plain = stats.render_team_md(s)
    assert plain == stats.render_team_md(s, weekly=None)
    assert "8-week trend" not in plain
    md = stats.render_team_md(s, weekly=[row(done=2, coverage=0.5)])
    assert md.startswith(plain)
    assert "\n## 8-week trend\n| week |" in md and "| 50% |" in md


# --- weak verdicts --------------------------------------------------------------


IN, AT = ts(10), ts(12)


def _verified(iid, tasks=None, at=AT, entered=IN, who="bob", word="verified"):
    i = issue(
        iid,
        labels=["Verify"],
        transitions=[(entered, "add", "Verify")],
        verdicts=[(at, who, word)],
    )
    if tasks is not None:
        i["tasks"] = list(tasks)
    return i


def test_weak_verdict_flags_unticked_steps_and_names_the_verifier():
    weak = stats.weak_verdicts([_verified(1, tasks=(1, 4))], START, END)
    assert [(w["iid"], w["verifier"], w["reasons"]) for w in weak] == [
        (1, "bob", ["steps 1/4"])
    ]


def test_weak_verdict_clean_cases_and_minutes_in_verify():
    history = [_verified(1, tasks=(3, 3)), _verified(2, tasks=(0, 0)), _verified(3)]
    assert stats.weak_verdicts(history, START, END) == [], "ticked/no list/old dump"
    failed = _verified(4, tasks=(0, 3), word="failed")
    overturned = _verified(5, tasks=(0, 3))
    overturned["verdicts"].append([ts(13), "cat", "failed"])
    assert stats.weak_verdicts([failed, overturned], START, END) == [], "not verified"
    i = _verified(6, entered="2026-09-12T00:00:00Z", at="2026-09-12T00:04:00Z")
    assert stats.weak_verdicts([i], START, END)[0]["reasons"] == ["4 min in Verify"]


def test_weak_verdicts_reach_summary_person_row_and_markdown():
    history = [_verified(1, tasks=(0, 2))]
    s = stats.summarise(history, COLUMNS, START, END, NOW)
    assert s["verify"]["weak_by"] == {"bob": 1}
    assert [w["iid"] for w in stats.for_person(s, history, "bob", NOW)["weak"]] == [1]
    assert stats.for_person(s, history, "ana", NOW)["weak"] == []
    assert stats.stat_row(s, "g/p", "b", "t")["weak"] == 1
    assert "steps 0/2" in stats.render_team_md(s)
    person = stats.for_person(s, history, "bob", NOW)
    assert "steps 0/2" in stats.render_person_md(person, s, "bob").split("\n---\n")[0]


TIGHT = {
    "iid": 7, "title": "slow one", "assignee": "bob", "due": "2026-09-17",
    "expected": "2026-09-19", "basis": "p85 of 5 cards: bob", "url": "http://x/7",
}  # fmt: skip


def test_tight_dates_reach_person_row_and_markdown():
    s = stats.summarise(history(), COLUMNS, START, END, NOW)
    assert stats.for_person(s, history(), "bob", NOW)["tight"] == []  # key absent
    s["flow"]["tight"] = [TIGHT]
    bob = stats.for_person(s, history(), "bob", NOW)
    assert bob["tight"] == [TIGHT]
    assert stats.for_person(s, history(), "alice", NOW)["tight"] == []
    assert stats.stat_row(s, "g/p", "b", "t")["tight"] == 1
    assert "| 7 | slow one | bob | 2026-09-17 | 2026-09-19 |" in stats.render_team_md(s)
    mine = stats.render_person_md(bob, s, "bob").split("\n---\n")[0]
    assert "| 7 | slow one | 2026-09-17 | 2026-09-19 |" in mine


LATE = {
    "milestone": "Beta", "due": "2026-09-20", "expected": "2026-09-22",
    "days_late": 2, "cards": 2, "unestimated": 1,
}  # fmt: skip


def test_late_milestones_reach_row_and_markdown_only_when_present():
    s = stats.summarise(history(), COLUMNS, START, END, NOW)
    assert stats.stat_row(s, "g/p", "b", "t")["late_milestones"] == 0
    assert "Late milestones" not in stats.render_team_md(s)
    s["flow"]["late_milestones"] = [LATE]
    assert stats.stat_row(s, "g/p", "b", "t")["late_milestones"] == 1
    assert (
        "| Beta | 2026-09-20 | 2026-09-22 | at least 2 days late, 1 without estimate |"
        in stats.render_team_md(s)
    )


# --- blocker flags ---------------------------------------------------------------

BLOCKED_COLUMNS = ["Doing", "Blocked", "Review", "Verify", "Done", "Failed"]


def blk(iid, labels=("Doing",), blocked_by=(), priority=None, milestone=None, **kw):
    """A history card with the blocker fields, as fetch_history now dumps it."""
    i = issue(iid, labels=labels, **{"assignee": "alice", **kw})
    i.update(
        priority=priority,
        milestone=milestone,
        milestone_due=None,
        blocked_by=[{"ref": r, "state": st, "since": None} for r, st in blocked_by],
    )
    return i


def blockers():
    return [
        blk(1, priority=3, due_date="2026-11-05", assignee=None),
        blk(
            2,
            priority=1,
            due_date="2026-10-20",
            blocked_by=[("1", "opened")],
            milestone="Beta",
        ),
        blk(3, labels=["Blocked"], blocked_by=[("9", "closed")], assignee="bob"),
    ]


def flagged(d):
    return {k: [x["iid"] for x in d[k]] for k in stats.FLAGS}


def test_summarise_flags_blockers():
    f = stats.summarise(blockers(), BLOCKED_COLUMNS, START, END, NOW)["flow"]
    assert flagged(f) == {
        "blocked_stale": [3],
        "blocked_unmarked": [2],
        "priority_inversion": [2],
        "date_inversion": [2],
        "unowned_blocker": [1],
        "no_milestone": [1, 3],
    }
    assert f["priority_inversion"][0]["detail"] == "#2 (P1) waits on #1 (P3)"
    s = stats.summarise(blockers(), BLOCKED_COLUMNS, START, END, NOW)
    r = stats.stat_row(s, "g/p", "b", "t")
    assert [r[k] for k in stats.FLAGS] == [1, 1, 1, 1, 1, 2], "row counts"
    assert all(stats.stat_row({}, "g/p", "b", "t")[k] == 0 for k in stats.FLAGS)


def test_old_dump_without_blocked_by_has_no_flags():
    # a card in Blocked in a pre-links dump is unknown, not "no open blocker"
    old = [*history(), issue(40, labels=["Blocked"], assignee="bob")]
    f = stats.summarise(old, BLOCKED_COLUMNS, START, END, NOW)["flow"]
    assert all(f[k] == [] for k in stats.FLAGS)


def test_for_person_filters_flags():
    h = blockers()
    s = stats.summarise(h, BLOCKED_COLUMNS, START, END, NOW)
    # an unowned blocker has no assignee by definition: it reaches the person
    # whose card waits on it
    assert flagged(stats.for_person(s, h, "alice", NOW)) == {
        "blocked_stale": [],
        "blocked_unmarked": [2],
        "priority_inversion": [2],
        "date_inversion": [2],
        "unowned_blocker": [1],
        "no_milestone": [],
    }
    bob = flagged(stats.for_person(s, h, "bob", NOW))
    assert bob["blocked_stale"] == [3] and bob["unowned_blocker"] == []


def test_three_moves_includes_unblock_after_overdue():
    def it(iid, **kw):
        return {"iid": iid, "title": f"t{iid}", "url": f"u{iid}", **kw}

    person = {
        "verify_queue": [it(1, days=2.0)],
        "overdue": [it(2, due="2026-09-01")],
        "questions": [it(3, days=1.0, author="root")],
        "unowned_blocker": [it(5, detail="#5 (unassigned) blocks #6 in Beta")],
        "priority_inversion": [it(6, detail="#6 (P1) waits on #7 (P3)")],
    }
    assert [(m["verb"], m["iid"], m["age"]) for m in stats.three_moves(person)] == [
        ("Verify", 1, "2.0 d in Verify"),
        ("Finish", 2, "due 2026-09-01"),
        ("Unblock", 5, "#5 (unassigned) blocks #6 in Beta"),
    ]
    person["unowned_blocker"] = []
    assert stats.three_moves(person)[2]["age"] == "#6 (P1) waits on #7 (P3)"


def test_team_markdown_has_blockers_section_only_when_flagged():
    h = blockers()
    s = stats.summarise(h, BLOCKED_COLUMNS, START, END, NOW)
    md = stats.render_team_md(s)
    assert "### Blockers\n| kind | card | detail |" in md
    assert "| priority_inversion | #2 issue 2 | #2 (P1) waits on #1 (P3) |" in md
    mine = stats.render_person_md(stats.for_person(s, h, "bob", NOW), s, "bob")
    assert "## Your blockers" in mine.split("\n---\n")[0]
    clean = stats.summarise(history(), COLUMNS, START, END, NOW)
    assert "Blockers" not in stats.render_team_md(clean)
    alice = stats.for_person(clean, history(), "alice", NOW)
    assert "blockers" not in stats.render_person_md(alice, clean, "alice")


def test_by_milestone_tally_and_old_row():
    beta = {"milestone": "Beta", "milestone_due": "2026-11-01"}
    h = [
        {**issue(1, created=12), **beta},
        {**issue(2, created=2), **beta},
        {**issue(3, created=3, closed=14), **beta},
        {**issue(4, created=13), "milestone": "Alpha", "milestone_due": "2026-10-01"},
        issue(5),
        {**issue(6, created=2, labels=["Done"]), **beta},  # in Done: not open
    ]
    s = stats.summarise(h, COLUMNS, START, END, NOW)
    assert s["by_milestone"] == {
        "Beta": {"open": 2, "done": 1, "added": 1, "due": "2026-11-01"},
        "Alpha": {"open": 1, "done": 0, "added": 1, "due": "2026-10-01"},
    }
    md = stats.render_team_md(s)
    assert md.index("Alpha (due 2026-10-01): 1 open, 0 done, +1 added") < md.index(
        "Beta (due 2026-11-01): 2 open, 1 done, +1 added"
    )
    assert stats.stat_row(s, "g/p", "dev", "t")["by_milestone"] == s["by_milestone"]
    assert stats.stat_row({}, "g/p", None, "t")["by_milestone"] == {}
    none = stats.summarise(history(), COLUMNS, START, END, NOW)
    assert none["by_milestone"] == {}
    assert "By milestone" not in stats.render_team_md(none), "absent when empty"
