"""Tests for replay.py — frames from hand-built batches, the page, the command."""

import json
import re
from datetime import UTC, datetime, timedelta

from typer.testing import CliRunner

from gitboard import replay
from gitboard.cli import app


def rec(iid, columns=("Doing",), title=None, assignee=None, project="g/p"):
    return {
        "ts": "",
        "project": project,
        "iid": iid,
        "title": title or f"issue {iid}",
        "assignee": assignee,
        "due_date": None,
        "columns": list(columns),
    }


def batch(ts, *recs):
    return {r["iid"]: {**r, "ts": ts} for r in recs}


def test_move_new_close_counts():
    b = [
        batch("t1", rec(1, ["Doing"]), rec(2, ["Backlog"])),
        batch("t2", rec(1, ["Review"]), rec(3, ["Backlog"])),
    ]
    frames, _ = replay.frames(b)
    assert [(f["ts"], f["moved"], f["new"], f["closed"]) for f in frames] == [
        ("t1", 0, 0, 0),
        ("t2", 1, 1, 1),
    ]
    assert frames[1]["columns"] == {"Backlog": [3], "Doing": [], "Review": [1]}


def test_columns_backlog_first_then_first_seen_and_stable():
    b = [
        batch("t1", rec(1, ["Doing"]), rec(2, ["Review"])),
        batch("t2", rec(1, ["Backlog"])),
    ]
    frames, _ = replay.frames(b)
    for f in frames:
        assert list(f["columns"]) == ["Backlog", "Doing", "Review"]


def test_two_column_card_in_both_lanes():
    b = [batch("t1", rec(1, ["Doing"])), batch("t2", rec(1, ["Doing", "Blocked"]))]
    frames, _ = replay.frames(b)
    assert frames[1]["columns"]["Doing"] == [1]
    assert frames[1]["columns"]["Blocked"] == [1]
    assert frames[1]["moved"] == 1


def test_identical_frames_collapse():
    b = [
        batch("t1", rec(1)),
        batch("t2", rec(1)),
        batch("t3", rec(1, ["Review"])),
        batch("t4", rec(1, ["Review"])),
    ]
    frames, _ = replay.frames(b)
    assert [f["ts"] for f in frames] == ["t1", "t3"]


def test_cards_hold_latest_values():
    b = [
        batch("t1", rec(1, title="old")),
        batch("t2", rec(1, ["Review"], title="new", assignee="al")),
    ]
    _, cards = replay.frames(b)
    assert cards == {1: {"title": "new", "assignee": "al", "due_date": None}}


def page(title="x", base="https://gl.example"):
    b = [
        batch("t1", rec(1, title=title)),
        batch("t2", rec(1, ["Review"], title=title)),
    ]
    frames, cards = replay.frames(b)
    return replay.render_html(frames, cards, "board", base, "g/p")


def test_page_is_self_contained_and_colours_columns():
    html = page()
    assert "https://cdn" not in html and "src=" not in html
    assert 'type="range"' in html
    assert "#3987e5" in html  # Doing, from mail.COLUMN_COLORS
    assert "prefers-color-scheme: dark" in html


def test_script_title_cannot_break_out():
    html = page(title="</script><script>alert(1)</script>&")
    assert html.count("</script>") == 1
    assert "<script>alert" not in html


def test_page_title_escaped():
    frames, cards = replay.frames([batch("t1", rec(1)), batch("t2", rec(1, ["R"]))])
    assert "&lt;b&gt;" in replay.render_html(frames, cards, "<b>")


def test_links_only_for_http():
    assert "https://gl.example/g/p/-/issues/1" in page()
    assert "javascript:" not in page(base="javascript:alert(1)//")
    assert "/-/issues/" not in page(base=None)


# --- CLI ---------------------------------------------------------------

runner = CliRunner()


def write_log(path, batches):
    path.write_text("".join(json.dumps(r) + "\n" for b in batches for r in b.values()))


def stamps():
    now = datetime.now(UTC)
    return (now - timedelta(hours=2)).isoformat(), now.isoformat()


def test_cli_writes_page(tmp_path):
    t1, t2 = stamps()
    log, out = tmp_path / "s.jsonl", tmp_path / "r.html"
    write_log(
        log, [batch(t1, rec(1, title="</script>")), batch(t2, rec(1, ["Review"]))]
    )
    r = runner.invoke(app, ["replay", "--db", str(log), "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert re.search(r"<title>g/p", out.read_text())
    assert out.read_text().count("</script>") == 1


def test_cli_needs_two_snapshots(tmp_path):
    log, out = tmp_path / "s.jsonl", tmp_path / "r.html"
    write_log(log, [batch(stamps()[1], rec(1))])
    r = runner.invoke(app, ["replay", "--db", str(log), "--out", str(out)])
    assert r.exit_code == 1
    assert "at least 2 snapshots" in r.output
    assert not out.exists()
    r = runner.invoke(app, ["replay", "--db", str(tmp_path / "none")])
    assert r.exit_code == 1


def test_cli_several_projects_need_a_name(tmp_path):
    t1, t2 = stamps()
    log = tmp_path / "s.jsonl"
    write_log(log, [batch(t1, rec(1), rec(2, project="g/q")), batch(t2, rec(1))])
    r = runner.invoke(app, ["replay", "--db", str(log)])
    assert r.exit_code == 1 and "g/q" in r.output


def test_cli_several_boards_need_board_and_board_keeps_one(tmp_path):
    now = datetime.now(UTC)
    t = [(now - timedelta(hours=h)).isoformat() for h in (4, 3, 2, 1)]

    def on(board, ts, *recs):
        return batch(ts, *({**r, "board": board} for r in recs))

    log, out = tmp_path / "s.jsonl", tmp_path / "r.html"
    write_log(
        log,
        [
            on("A", t[0], rec(1)),
            on("B", t[1], rec(2)),
            on("A", t[2], rec(1, ["Review"])),
            on("B", t[3], rec(2)),
        ],
    )
    r = runner.invoke(app, ["replay", "--db", str(log), "--out", str(out)])
    assert r.exit_code == 1 and "--board" in r.output
    r = runner.invoke(
        app, ["replay", "--db", str(log), "--out", str(out), "--board", "A"]
    )
    assert r.exit_code == 0, r.output
    assert "issue 2" not in out.read_text(), "board B's cards stay out"
