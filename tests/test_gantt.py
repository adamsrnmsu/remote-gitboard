"""Tests for gantt.py — bars from history, drawn as Outlook-safe cells."""

from datetime import date

from test_mail import NO_BG
from test_stats import COLUMNS, NOW, history, issue, ts

from gitboard import estimate, gantt

TODAY = NOW.date()
CFG = estimate.DEFAULTS


def by_iid(rows):
    return {b["iid"]: b for b in rows}


def test_bars_end_at_done_then_due_then_estimate_then_today():
    h = [
        issue(1, closed=14, labels=["Done"], assignee="a",
              transitions=[(ts(10), "add", "Doing")]),
        issue(2, labels=["Doing"], assignee="a", due_date="2026-09-10",
              transitions=[(ts(5), "add", "Doing")]),
        issue(3, labels=["Doing"], due_date="2026-09-20",
              transitions=[(ts(12), "add", "Doing")]),
        issue(4, labels=["Doing"], transitions=[(ts(15), "add", "Doing")]),
        issue(5),  # Backlog, nothing to place it by
    ]  # fmt: skip
    rows = by_iid(gantt.bars(h, COLUMNS, NOW, CFG))
    assert set(rows) == {1, 2, 3, 4}
    assert (rows[1]["status"], rows[1]["start"], rows[1]["end"]) == (
        "done", date(2026, 9, 10), date(2026, 9, 14),
    )  # fmt: skip
    assert (rows[2]["status"], rows[2]["end"], rows[2]["tag"]) == (
        "late", TODAY, "late 09-10",
    )  # fmt: skip
    assert (rows[3]["status"], rows[3]["end"]) == ("due", date(2026, 9, 20))
    assert (rows[4]["status"], rows[4]["end"]) == ("undated", TODAY)


def test_bars_forecast_from_history_and_skip_old_done():
    done = [
        issue(10 + k, created=1, closed=4, assignee="a",
              transitions=[(ts(1), "add", "Doing")])
        for k in range(5)
    ]  # fmt: skip
    old = dict(done[0], iid=99, closed_at="2026-07-01T00:00:00Z")
    card = issue(1, labels=["Doing"], assignee="a",
                 transitions=[(ts(15), "add", "Doing")])  # fmt: skip
    rows = by_iid(gantt.bars([*done, old, card], COLUMNS, NOW, CFG))
    assert 99 not in rows
    assert rows[1]["status"] == "est" and rows[1]["end"] == date(2026, 9, 18)


def test_current_milestone_is_soonest_due_with_open_cards():
    def b(ms, due, status="due"):
        return {"milestone": ms, "milestone_due": due, "status": status}

    rows = [b("v1", "2026-09-01", "done"), b("v2", "2026-10-01"), b("v3", None)]
    assert gantt.current_milestone(rows) == "v2"
    assert gantt.current_milestone([b("x", None), b("y", None), b("y", None)]) == "y"
    assert gantt.current_milestone([b(None, None)]) is None


def test_paint_fills_the_track_with_a_min_width_bar_clipped_and_marked():
    lo, hi = date(2026, 9, 1), date(2026, 9, 11)
    row = {"start": date(2026, 8, 1), "end": date(2026, 8, 2), "status": "due"}
    runs = gantt.paint(row, lo, hi, [date(2026, 9, 6)])
    assert sum(n for _, n in runs) == gantt.TRACK_W
    assert runs[0] == (gantt.STATUS["due"], 4)  # clipped to the edge, still seen
    assert (gantt.HACK["marker"], 2) in runs
    half = gantt.paint({**row, "end": hi, "done": 0.5}, lo, hi, [])
    assert half[0] == (gantt.STATUS["done"], gantt.TRACK_W // 2)


def test_charts_are_outlook_safe_escaped_and_empty_without_rows():
    h = history()
    h[3] = dict(h[3], title="<script>x</script>", milestone="M1",
                milestone_due="2026-09-30")  # fmt: skip
    rows = gantt.bars(h, COLUMNS, NOW, CFG)
    blocks = gantt.team_blocks(rows, [], TODAY, {"alice", "bob"})
    html = "".join(blocks)
    assert len(blocks) == 4  # project, milestone, alice, bob
    assert NO_BG.findall(html) == []
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "$ gantt --milestone &#x27;M1&#x27;" in html
    (project,) = gantt.groups(rows, [], TODAY)
    assert project["status"] == "late"  # #4 is past its due date
    assert gantt.person_chart(rows, "nobody", TODAY) == ""
    many = [dict(rows[0], iid=k) for k in range(20)]
    assert "+8 more" in gantt.chart("x", many, TODAY)
    assert "more" not in gantt.chart("x", many, TODAY, cap=None)
    assert gantt.LABEL_W + gantt.TRACK_W + gantt.TAG_W == 600


def test_chart_text_is_ascii_with_hand_checkable_bars():
    today = date(2026, 9, 14)
    rows = [
        {"iid": 1, "title": "first ✓ thing", "start": date(2026, 9, 4), "end": date(2026, 9, 6), "tag": "done 09-06"},
        {"iid": 2, "title": "x" * 80, "start": date(2026, 9, 10), "end": today, "tag": "late 09-12"},
    ]  # fmt: skip
    text = gantt.chart_text("gantt --who a", rows, today)
    head, axis, one, two = text.split("\n")
    assert head == "$ gantt --who a  # 2 rows"
    assert axis.endswith("Sep 4 .. Sep 14")  # 11 days across 30 columns
    assert "|" + "#" * 8 + "." * 22 + "|  Sep 4 - Sep 6  done 09-06" in one
    assert "|" + "." * 16 + "#" * 14 + "|  Sep 10 - Sep 14  late 09-12" in two
    assert text.isascii() and max(map(len, text.split("\n"))) <= gantt.LINE_MAX
    assert gantt.chart_text("x", [], today) == ""
    assert "+8 more" in gantt.chart_text("x", [rows[0]] * 20, today)


def test_a_card_done_exactly_at_the_lookback_edge_is_kept():
    edge = TODAY - gantt.LOOKBACK
    done = issue(1, labels=["Done"], assignee="a",
                 transitions=[(ts(10), "add", "Doing")])  # fmt: skip
    done["closed_at"] = f"{edge.isoformat()}T00:00:00Z"
    assert 1 in by_iid(gantt.bars([done], COLUMNS, NOW, CFG))


def test_paint_keeps_a_marker_on_the_last_day_inside_the_track():
    lo, hi = (
        date(2026, 9, 1),
        date(2028, 8, 1),
    )  # 700 days: the last day rounds to px 330
    row = {"start": lo, "end": lo, "status": "due"}
    runs = gantt.paint(row, lo, hi, [date(2028, 7, 31)])
    assert sum(n for _, n in runs) == gantt.TRACK_W
    assert runs[-1] == (gantt.HACK["marker"], 2)


def test_dues_prefers_the_listed_milestone_date_over_a_card_copy():
    ms = [{"title": "A", "due_date": "2026-09-01"}, {"title": "B", "due_date": None}]
    rows = [
        {"milestone": "A", "milestone_due": "2026-10-01"},
        {"milestone": "B", "milestone_due": "2026-10-02"},
    ]
    assert gantt._dues(rows, ms) == {"A": "2026-09-01", "B": "2026-10-02"}


def test_group_is_late_on_either_past_due_or_a_card_ending_after_the_due_date():
    def card(end, ms, status="due"):
        return {"milestone": ms, "epic": None, "status": status, "end": end,
                "start": date(2026, 9, 1), "milestone_due": None}  # fmt: skip

    ms = [{"title": "Past", "due_date": "2026-09-10"},
          {"title": "Future", "due_date": "2026-09-30"}]  # fmt: skip
    rows = [
        card(date(2026, 9, 5), "Past"),  # open, due date passed (TODAY 09-16)
        card(date(2026, 10, 5), "Future"),  # open, ends after its due date
        card(date(2026, 9, 20), "Future", "done"),
    ]
    got = {g["title"]: g["status"] for g in gantt.groups(rows, ms, TODAY)}
    assert got == {"Past": "late", "Future": "late"}
    rows = [card(date(2026, 9, 5), "Future"), card(date(2026, 9, 6), "Future")]
    assert gantt.groups(rows, ms, TODAY)[0]["status"] == "due", "neither"
