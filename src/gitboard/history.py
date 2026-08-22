"""The local progress log: board snapshots in SQLite.

SQLite over JSONL because this is meant to run on a schedule for months:
change-detection keeps it an event log (a run that changes nothing writes
nothing rather than another 200 identical lines), and the analysis later is
SQL instead of a jq pipeline. Stdlib only.
"""

import sqlite3
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    ts       TEXT NOT NULL,
    project  TEXT NOT NULL,
    board    TEXT NOT NULL,
    iid      INTEGER NOT NULL,
    title    TEXT NOT NULL,
    assignee TEXT,
    due_date TEXT,
    columns  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS snapshot_issue ON snapshot (project, iid, ts);
"""

TRACKED = ("title", "assignee", "due_date", "columns")


def record(db_path, records):
    """Append the rows that changed since each issue's last row.

    The first run stores everything; after that only movement is written.
    Returns the number of rows written.
    """
    rows = [{**r, "columns": ",".join(r["columns"])} for r in records]
    with closing(sqlite3.connect(db_path)) as db, db:
        db.executescript(SCHEMA)
        written = 0
        for row in rows:
            last = db.execute(
                "SELECT title, assignee, due_date, columns FROM snapshot"
                " WHERE project = ? AND iid = ?"
                " ORDER BY ts DESC, rowid DESC LIMIT 1",
                (row["project"], row["iid"]),
            ).fetchone()
            if last == tuple(row[k] for k in TRACKED):
                continue
            db.execute(
                "INSERT INTO snapshot"
                " (ts, project, board, iid, title, assignee, due_date, columns)"
                " VALUES (:ts, :project, :board, :iid, :title, :assignee,"
                " :due_date, :columns)",
                row,
            )
            written += 1
    return written
