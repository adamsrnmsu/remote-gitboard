"""Tests for history.py — the SQLite snapshot log. Real sqlite, tmp file."""

import sqlite3

from gitboard import history


def rec(iid, columns=("Doing",), ts="2026-08-21T00:00:00+00:00", **over):
    base = {
        "ts": ts,
        "project": "grp/proj",
        "board": "Dev Board",
        "iid": iid,
        "title": f"issue {iid}",
        "assignee": None,
        "due_date": None,
        "columns": list(columns),
    }
    return {**base, **over}


def rows(db_path):
    with sqlite3.connect(db_path) as db:
        return db.execute("SELECT iid, columns FROM snapshot ORDER BY rowid").fetchall()


def test_first_run_records_everything(tmp_path):
    db = tmp_path / "s.db"
    assert history.record(db, [rec(1), rec(2)]) == 2
    assert rows(db) == [(1, "Doing"), (2, "Doing")]


def test_unchanged_board_writes_nothing(tmp_path):
    """The reason this is SQLite: a cron run with no movement costs no rows."""
    db = tmp_path / "s.db"
    history.record(db, [rec(1)])
    assert history.record(db, [rec(1, ts="2026-08-22T00:00:00+00:00")]) == 0
    assert len(rows(db)) == 1


def test_a_move_appends_and_keeps_history(tmp_path):
    db = tmp_path / "s.db"
    history.record(db, [rec(1, columns=["Doing"])])
    assert history.record(db, [rec(1, columns=["Done"], ts="t2")]) == 1
    assert rows(db) == [(1, "Doing"), (1, "Done")]
