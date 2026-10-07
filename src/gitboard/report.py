"""Read the snapshot log and say what moved. Local files only, no network.

The JSONL is append-only: one line per open issue per `snapshot` run. A
report diffs the earliest and latest runs inside a window, so it takes two
snapshots before it can say anything about movement — which is the honest
minimum anyway.
"""

import json
import os
import subprocess
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta


def load(path, project=None, days=7):
    """Snapshot batches inside the window, oldest first: [{iid: record}]."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    batches = defaultdict(dict)
    try:
        with open(path) as f:
            for line in f:
                rec = json.loads(line)
                if rec["ts"] < cutoff:
                    continue
                if project and rec["project"] != project:
                    continue
                batches[rec["ts"]][rec["iid"]] = rec
    except FileNotFoundError:
        return []
    return [batches[ts] for ts in sorted(batches)]


def diff(batches):
    """First batch vs last: what appeared, went away, moved, or sat still.

    An issue gone from the latest snapshot was closed (snapshot records only
    open issues), which is the nearest thing the log has to "shipped".
    """
    first, last = batches[0], batches[-1]
    return {
        "new": [last[i] for i in sorted(last.keys() - first.keys())],
        "closed": [first[i] for i in sorted(first.keys() - last.keys())],
        "moved": [
            (first[i], last[i])
            for i in sorted(first.keys() & last.keys())
            if first[i]["columns"] != last[i]["columns"]
        ],
        "unchanged": [
            last[i]
            for i in sorted(first.keys() & last.keys())
            if first[i]["columns"] == last[i]["columns"]
        ],
    }


def by_assignee(changes):
    """{assignee: Counter} — activity attributed to the issue's assignee."""
    tally = defaultdict(Counter)
    for rec in changes["new"]:
        tally[rec["assignee"] or "unassigned"]["new"] += 1
    for rec in changes["closed"]:
        tally[rec["assignee"] or "unassigned"]["closed"] += 1
    for _, after in changes["moved"]:
        tally[after["assignee"] or "unassigned"]["moved"] += 1
    return dict(tally)


def verifier_counts(history, start, end):
    """{username: verdicts} from a stats --dump's history, inside [start, end].

    A verdict is `[ts, author, kind]`; the author is who checked the work,
    which the snapshot log cannot see — it only knows the assignee.
    """
    tally = Counter()
    for issue in history:
        for ts, author, _ in issue["verdicts"]:
            at = datetime.fromisoformat(ts)
            if start <= at <= end:
                tally[author] += 1
    return tally


def commit_counts(repo, days):
    """Commits per (author, email) over the same window, via `git log`."""
    done = subprocess.run(
        ["git", "-C", repo, "log", f"--since={days} days ago", "--format=%an\t%ae"],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        raise OSError(done.stderr.strip() or f"git log failed in {repo}")
    tally = Counter()
    for line in done.stdout.splitlines():
        name, _, email = line.partition("\t")
        tally[(name, email)] += 1
    return tally


def match_author(assignee, authors):
    """GitLab username -> git author, by name or the email's local part."""
    # ponytail: heuristic join; add a username->email map to the config
    # when a real team's names stop matching
    low = assignee.lower()
    for name, email in authors:
        if low in (name.lower(), email.split("@")[0].lower()):
            return (name, email)
    return None


def since(batches, ts):
    """Batches taken at or after `ts` (an ISO string; snapshots compare as text)."""
    return [b for b in batches if b and next(iter(b.values()))["ts"] >= ts]


def away(batches, since_ts, now=None, board=None):
    """Counts of what changed since `since_ts` (ISO text), or {} for nothing.

    Compares the last batch at or before `since_ts` (the board as you left it)
    with the latest one. Counts only; no assignee, no names. `board` limits
    to one board of the project, as `load` only filters by project.
    """
    now = now or datetime.now(UTC)
    if board:
        batches = [{i: r for i, r in b.items() if r["board"] == board} for b in batches]
    batches = [b for b in batches if b]
    then = [b for b in batches if next(iter(b.values()))["ts"] <= since_ts]
    if not then or batches[-1] is then[-1]:
        return {}
    before, after = then[-1], batches[-1]
    was = datetime.fromisoformat(since_ts).date().isoformat()
    today = now.date().isoformat()
    moved = [
        i
        for i in before.keys() & after.keys()
        if before[i]["columns"] != after[i]["columns"]
    ]
    counts = {
        "moved": len(moved),
        "new": len(after.keys() - before.keys()),
        "to Done": sum(
            "Done" in after[i]["columns"] and "Done" not in before[i]["columns"]
            for i in moved
        ),
        "closed": len(before.keys() - after.keys()),
        "newly overdue": sum(
            bool(r["due_date"]) and was <= str(r["due_date"]) < today
            for r in after.values()
        ),
    }
    return {k: n for k, n in counts.items() if n}


def describe(counts, since):
    """`since Tue 14:02: 6 moved · 2 new`, or None when there is nothing."""
    if not counts:
        return None
    at = since.astimezone().strftime("%a %H:%M")
    return f"since {at}: " + " · ".join(f"{n} {k}" for k, n in counts.items())


def seen_path():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "gitboard", "seen.json")


def _seen():
    try:
        with open(seen_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def last_seen(key):
    """When this board (`project/board`) was last left, or None."""
    try:
        return datetime.fromisoformat(_seen()[key])
    except (KeyError, TypeError, ValueError):
        return None


def mark_seen(key, at=None):
    """Record `at` (default now, UTC) for `key`; never raises."""
    data = _seen()
    data[key] = (at or datetime.now(UTC)).isoformat(timespec="seconds")
    try:
        os.makedirs(os.path.dirname(seen_path()), exist_ok=True)
        with open(seen_path(), "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def column_ages(batches):
    """{iid: (column, ts)} — where each open issue is now, and since when.

    A streak is consecutive batches with the same columns list; the ts is the
    first batch of the current streak. Only issues in the latest batch are
    kept. Two-column issues report `Doing+Blocked`, as `report` prints them.
    """
    ages = {}
    for batch in batches:
        nxt = {}
        for iid, rec in batch.items():
            col = "+".join(rec["columns"])
            prev = ages.get(iid)
            nxt[iid] = prev if prev and prev[0] == col else (col, rec["ts"])
        ages = nxt
    return ages


def age_days(ages, now=None):
    """{iid: (column, days)} — column_ages, with the ts turned into whole days."""
    now = now or datetime.now(UTC)
    return {
        iid: (col, (now - datetime.fromisoformat(ts)).days)
        for iid, (col, ts) in ages.items()
    }


STUCK = {"Verify": 1, "Doing": 3}


def stuck(ages, thresholds=None, now=None):
    """[(iid, column, days)] for issues past their column's threshold, oldest first.

    A two-column issue is stuck if any of its columns is over threshold.
    """
    thresholds = STUCK if thresholds is None else thresholds
    hits = [
        (iid, col, days)
        for iid, (col, days) in age_days(ages, now).items()
        if any(days >= thresholds[c] for c in col.split("+") if c in thresholds)
    ]
    return sorted(hits, key=lambda h: (-h[2], h[0]))
