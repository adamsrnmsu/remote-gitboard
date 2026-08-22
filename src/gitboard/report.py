"""Read the snapshot log and say what moved. Local files only, no network.

The JSONL is append-only: one line per open issue per `snapshot` run. A
report diffs the earliest and latest runs inside a window, so it takes two
snapshots before it can say anything about movement — which is the honest
minimum anyway.
"""

import json
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
