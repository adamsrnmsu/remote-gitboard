"""Board statistics over an issue history. Pure: no network; stdlib plus
`graph` for the blocker flags.

Input is what `board.fetch_history` returns — plain dicts with ISO
timestamps, label `transitions` (column labels only), `verdicts` and
`notes` — so everything here runs offline from a dumped JSON file.
"""

import json
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from statistics import mean, median

from gitboard import graph, ingest, report

FLAGS = graph.FLAGS
VERIFY, REVIEW, DONE, FAILED = "Verify", "Review", "Done", "Failed"
BACKLOG = "Backlog"
NOT_WIP = {BACKLOG, DONE, FAILED}
QUESTION = "Q:"
DAY = timedelta(days=1)
FAST_VERIFY = timedelta(minutes=10)


def parse_ts(s):
    """ISO-8601 string (Z or offset) -> aware UTC datetime; None passes through."""
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def scopes(labels, scope):
    """Values of every `scope::x` label, sorted."""
    pre = f"{scope}::"
    return sorted(x[len(pre) :] for x in labels if x.startswith(pre))


def scoped(labels, scope):
    """The first `scope::x` value, or None."""
    return (scopes(labels, scope) or [None])[0]


def dwell(issue, label):
    """[(entered, left|None)] — every stay in `label`, from the transitions.

    A `remove` with no preceding `add` (or the label present with no events
    at all, as on instances older than 11.4) starts at `created_at`. An
    unmatched `add` ends at `closed_at` when the issue is closed, else it is
    still in progress (`None`).
    """
    created, closed = parse_ts(issue["created_at"]), parse_ts(issue.get("closed_at"))
    events = [
        (parse_ts(ts), act) for ts, act, lab in issue["transitions"] if lab == label
    ]
    out, start = [], None
    for ts, act in events:
        if act == "add" and start is None:
            start = ts
        elif act == "remove":
            out.append((start or created, ts))
            start = None
    if start is not None:
        out.append((start, closed))
    elif not events and label in issue["labels"]:
        out.append((created, closed))
    return out


def done_at(issue):
    """When the work finished: `closed_at`, else the last move into Done."""
    if issue.get("closed_at"):
        return parse_ts(issue["closed_at"])
    adds = [ts for ts, act, lab in issue["transitions"] if lab == DONE and act == "add"]
    return parse_ts(adds[-1]) if adds else None


def column_of(issue, columns):
    """Column label(s) the issue carries, joined by "+", else Backlog."""
    return "+".join(c for c in columns if c in issue["labels"]) or BACKLOG


def _days(a, b):
    # clock skew between the API and fetched_at can make this -0.0; a note
    # posted "just now" is 0.0 days old, not negative
    return max(0.0, round((b - a) / DAY, 1))


def _stat(values):
    """{median, mean, n}; an empty sample is None, not a fake zero."""
    if not values:
        return {"median": None, "mean": None, "n": 0}
    return {
        "median": round(median(values), 1),
        "mean": round(mean(values), 1),
        "n": len(values),
    }


def _in(ts, start, end):
    return ts is not None and start <= ts < end


def _done_in(history, start, end):
    """[(issue, done_ts)] finished inside the window."""
    pairs = ((i, done_at(i)) for i in history)
    return [(i, d) for i, d in pairs if _in(d, start, end)]


def _ended_in(history, label, start, end):
    """Durations (days) of every stay in `label` that ended in the window."""
    return [
        _days(a, b)
        for i in history
        for a, b in dwell(i, label)
        if b is not None and _in(b, start, end)
    ]


def _period(history, start, end):
    """The three trend figures for one window."""
    done = _done_in(history, start, end)
    return {
        "done": len(done),
        "cycle_median": _stat([_days(parse_ts(i["created_at"]), d) for i, d in done])[
            "median"
        ],
        "verify_median": _stat(_ended_in(history, VERIFY, start, end))["median"],
    }


def _questions(issue):
    """Waiting questions: a `Q:` note nobody else has answered since."""
    notes = sorted(issue["notes"])
    out = []
    for k, (ts, author, text) in enumerate(notes):
        if not text.lstrip().startswith(QUESTION):
            continue
        if any(a != author for _, a, _ in notes[k + 1 :]):
            continue
        out.append({"ts": ts, "author": author, "text": text.strip()})
    return out


def _entered(issue, columns):
    """Earliest open-interval start among the issue's current columns."""
    starts = [
        a
        for c in columns
        if c in issue["labels"]
        for a, b in dwell(issue, c)
        if b is None
    ]
    return min(starts) if starts else parse_ts(issue["created_at"])


def _in_verify(issue, t):
    """True if some Verify stay covers instant `t`."""
    return any(a <= t and (b is None or b > t) for a, b in dwell(issue, VERIFY))


def weak_verdicts(history, start, end):
    """`verified` verdicts in the window that look like a rubber stamp.

    Two reasons, either flags: the description's task list is not fully
    ticked (`steps 1/4`), or the verdict came within `FAST_VERIFY` of the
    card entering Verify. Only an issue's latest verdict counts, so a
    `verified` later overturned by `failed` is not reported. Ticks are the
    current state, not the state at verdict time: ticking the steps
    afterwards clears the flag. A flag, never a move — the verdict stays the
    person's.
    """
    out = []
    for i in history:
        if not i["verdicts"]:
            continue
        at, who, word = i["verdicts"][-1]
        at = parse_ts(at)
        if word != "verified" or not _in(at, start, end):
            continue
        reasons = []
        ticked, total = i.get("tasks") or (0, 0)
        if ticked < total:
            reasons.append(f"steps {ticked}/{total}")
        entered = [a for a, _ in dwell(i, VERIFY) if a <= at]
        if entered and at - entered[-1] < FAST_VERIFY:
            reasons.append(
                f"{int((at - entered[-1]).total_seconds() // 60)} min in Verify"
            )
        if reasons:
            out.append(
                {
                    "iid": i["iid"],
                    "title": i["title"],
                    "verifier": who,
                    "reasons": reasons,
                    "url": i["web_url"],
                }
            )
    return out


def daily_series(history, columns, start, end):
    """[{date, open, verify, done_cum}] per calendar day, counted at end of day.

    `open`: created by then and not yet done; `verify`: some Verify stay covers
    the day end; `done_cum`: finished since `start`. Same `done_at`/`dwell` as
    `summarise`, so the chart and the numbers agree.
    """
    rows = [(parse_ts(i["created_at"]), done_at(i), i) for i in history]
    d = datetime.combine(start.date(), time(), tzinfo=UTC)
    last = datetime.combine(end.date(), time(), tzinfo=UTC)
    out = []
    while d <= last:
        d1 = d + DAY
        out.append(
            {
                "date": d.date().isoformat(),
                "open": sum(
                    1
                    for c, dn, _ in rows
                    if (c is None or c <= d1) and (dn is None or dn > d1)
                ),
                "verify": sum(1 for _, _, i in rows if _in_verify(i, d1)),
                "done_cum": sum(1 for _, dn, _ in rows if _in(dn, start, d1)),
            }
        )
        d = d1
    return out


def momentum(summary):
    """One sentence on done and the verify queue against the previous window."""
    prev, now = summary["trend"]["done"]
    if prev is None:
        text = f"Done {now}."
    elif now > prev:
        text = f"Done {now}, up from {prev}."
    elif now < prev:
        text = f"Done {now}, down from {prev}."
    else:
        text = f"Done {now}, flat."
    qprev, qnow = summary["trend"].get("verify_queue", (None, None))
    if qprev is None:
        return text
    word = "shrinking" if qnow < qprev else "growing" if qnow > qprev else "flat"
    bang = "!" if prev is not None and now > prev and qnow < qprev else "."
    return f"{text} Verify queue {qnow} (was {qprev}) — {word}{bang}"


def three_moves(person):
    """Oldest verify, first overdue, first unblock, first question, then more
    verify; at most 3."""

    def move(verb, item, age):
        return {
            "verb": verb,
            "iid": item["iid"],
            "title": item["title"],
            "age": age,
            "url": item["url"],
        }

    queue = [
        move("Verify", q, f"{q['days']} d in Verify") for q in person["verify_queue"]
    ]
    overdue = [move("Finish", o, f"due {o['due']}") for o in person["overdue"]]
    unblock = [
        move("Unblock", x, x["detail"])
        for k in ("unowned_blocker", "priority_inversion")
        for x in person.get(k, [])
    ]
    questions = [
        move("Answer", q, f"asked {q['days']} d ago by {q['author']}")
        for q in person["questions"]
    ]
    # one row per card: a card that is overdue *and* in Verify is one move, so
    # each bucket contributes its first card not already picked
    picked, seen = [], set()
    for bucket in (queue[:1], overdue, unblock, questions, queue[1:]):
        for m in bucket:
            if m["iid"] not in seen:
                seen.add(m["iid"])
                picked.append(m)
                break
    for m in queue:
        if len(picked) >= 3:
            break
        if m["iid"] not in seen:
            seen.add(m["iid"])
            picked.append(m)
    return picked[:3]


def summarise(history, columns, start, end, now):
    """Team numbers for [start, end); `trend` compares against the window before."""
    opened = [i for i in history if i["state"] == "opened"]
    today = now.date().isoformat()

    def tally(issues, key):
        return dict(Counter(k for i in issues if (k := key(i)) is not None))

    def col(i):
        return column_of(i, columns)

    done = _done_in(history, start, end)
    done_issues = [i for i, _ in done]
    queue = sorted(
        (
            {
                "iid": i["iid"],
                "title": i["title"],
                "assignee": i["assignee"],
                "days": _days(_entered(i, [VERIFY]), now),
                "url": i["web_url"],
            }
            for i in opened
            if VERIFY in i["labels"]
        ),
        key=lambda q: (-q["days"], q["iid"]),
    )
    in_verify_at_start = sum(1 for i in history if _in_verify(i, start))
    overdue = [
        {
            "iid": i["iid"],
            "title": i["title"],
            "assignee": i["assignee"],
            "due": i["due_date"],
            "url": i["web_url"],
        }
        for i in opened
        if i.get("due_date") and i["due_date"] < today and col(i) != DONE
    ]
    verdicts = [
        v for i in history for v in i["verdicts"] if _in(parse_ts(v[0]), start, end)
    ]
    ages = {
        i["iid"]: (col(i), _entered(i, columns).isoformat())
        for i in opened
        if col(i) != BACKLOG
    }
    wip = tally(
        [i for i in opened if col(i) not in NOT_WIP and i["assignee"]],
        lambda i: i["assignee"],
    )
    questions = [
        {
            "iid": i["iid"],
            "title": i["title"],
            "assignee": i["assignee"],
            **q,
            "days": _days(parse_ts(q["ts"]), now),
            "url": i["web_url"],
        }
        for i in opened
        for q in _questions(i)
    ]
    by_milestone = {}
    for i in history:
        if m := i.get("milestone"):
            by_milestone.setdefault(
                m, {"open": 0, "done": 0, "added": 0, "due": i.get("milestone_due")}
            )
    for i in opened:
        if i.get("milestone") and DONE not in i["labels"]:
            by_milestone[i["milestone"]]["open"] += 1
    for m, n in tally(done_issues, lambda i: i.get("milestone")).items():
        by_milestone[m]["done"] = n
    # ponytail: counts by created_at, so a card assigned later still counts as
    # added when created; exact needs milestone events fetched
    added = [i for i in history if _in(parse_ts(i["created_at"]), start, end)]
    for m, n in tally(added, lambda i: i.get("milestone")).items():
        by_milestone[m]["added"] = n
    prev = _period(history, start - (end - start), start)
    cur = _period(history, start, end)
    weak = weak_verdicts(history, start, end)
    return {
        "period": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "days": (end - start).days,
        },
        "columns": list(columns),
        "by_milestone": by_milestone,
        "open": {
            "total": len(opened),
            "by_column": tally(opened, col),
            "by_epic": tally(opened, lambda i: scoped(i["labels"], "epic")),
            "by_story": tally(opened, lambda i: scoped(i["labels"], "story")),
            "by_type": tally(opened, lambda i: scoped(i["labels"], "type")),
            "by_assignee": tally(opened, lambda i: i["assignee"]),
            "unassigned": sum(1 for i in opened if not i["assignee"]),
        },
        "throughput": {
            "opened": sum(
                1 for i in history if _in(parse_ts(i["created_at"]), start, end)
            ),
            "done": len(done),
            "closed": sum(
                1 for i in history if _in(parse_ts(i.get("closed_at")), start, end)
            ),
            "done_by": {
                "assignee": tally(done_issues, lambda i: i["assignee"] or "unassigned"),
                "epic": tally(done_issues, lambda i: scoped(i["labels"], "epic")),
                "story": tally(done_issues, lambda i: scoped(i["labels"], "story")),
            },
            "cycle_days": _stat([_days(parse_ts(i["created_at"]), d) for i, d in done]),
        },
        "verify": {
            "queue": queue,
            "oldest_days": queue[0]["days"] if queue else None,
            "verified": sum(1 for v in verdicts if v[2] == "verified"),
            "failed": sum(1 for v in verdicts if v[2] == "failed"),
            "verifiers": dict(Counter(v[1] for v in verdicts)),
            "weak": weak,
            "weak_by": dict(Counter(w["verifier"] for w in weak)),
            "verify_days": _stat(_ended_in(history, VERIFY, start, end)),
            "review_days": _stat(_ended_in(history, REVIEW, start, end)),
            "coverage": round(
                sum(1 for i in done_issues if dwell(i, VERIFY)) / len(done), 2
            )
            if done
            else None,
        },
        "flow": {
            "overdue": len(overdue),
            "overdue_items": overdue,
            "stuck": report.stuck(ages, report.STUCK, now),
            "stale": sum(1 for i in opened if ingest.STALE in i["labels"]),
            "reverify": sum(1 for i in opened if ingest.REVERIFY in i["labels"]),
            "wip": wip,
            "questions": questions,
            "multi_scope": sorted(
                i["iid"]
                for i in opened
                if any(len(scopes(i["labels"], s)) > 1 for s in ("epic", "story"))
            ),
            # a dump written before links were read has no `blocked_by` at
            # all; its Blocked cards are unknown, not stale
            **(
                graph.flags(history, columns)
                if any("blocked_by" in i for i in history)
                else {k: [] for k in FLAGS}
            ),
        },
        "trend": {
            **{k: (prev[k], cur[k]) for k in cur},
            "verify_queue": (in_verify_at_start, len(queue)),
        },
    }


def for_person(summary, history, username, now):
    """One assignee's slice: their queue, overdue, WIP, done, verdicts, questions."""
    start, end = (
        parse_ts(summary["period"]["start"]),
        parse_ts(summary["period"]["end"]),
    )
    mine = [i for i in history if i["assignee"] == username]
    opened = [i for i in mine if i["state"] == "opened"]
    today = now.date().isoformat()
    verdicts = [
        v
        for i in history
        for v in i["verdicts"]
        if v[1] == username and _in(parse_ts(v[0]), start, end)
    ]
    return {
        "verify_queue": [
            q for q in summary["verify"]["queue"] if q["assignee"] == username
        ],
        "overdue": [
            {
                "iid": i["iid"],
                "title": i["title"],
                "due": i["due_date"],
                "url": i["web_url"],
            }
            for i in opened
            if i.get("due_date") and i["due_date"] < today and DONE not in i["labels"]
        ],
        "open_by_column": dict(
            Counter(column_of(i, summary["columns"]) for i in opened)
        ),
        "done": [
            {"iid": i["iid"], "title": i["title"], "url": i["web_url"]}
            for i, _ in _done_in(mine, start, end)
        ],
        "verified": sum(1 for v in verdicts if v[2] == "verified"),
        "failed": sum(1 for v in verdicts if v[2] == "failed"),
        "weak": [
            w for w in summary["verify"].get("weak", []) if w["verifier"] == username
        ],
        "tight": [
            t for t in summary["flow"].get("tight", []) if t["assignee"] == username
        ],
        "questions": [
            q
            for q in summary["flow"]["questions"]
            if q["assignee"] == username and q["author"] != username
        ],
        **_my_flags(summary["flow"], history, username),
    }


def _my_flags(flow, history, username):
    """The flags on this person's cards. An unowned blocker has no assignee
    by definition, so it goes to whoever owns the card waiting on it."""
    owner = {str(i["iid"]): i["assignee"] for i in history}
    return {
        k: [
            x
            for x in flow.get(k, [])
            if (owner.get(x["blocker"]) if k == "unowned_blocker" else x["assignee"])
            == username
        ]
        for k in FLAGS
    }


# --- over time ---------------------------------------------------------------


def stat_row(summary, project, board, ts):
    """One JSONL line: the week's headline numbers, flat, for the trend log."""
    g = summary.get
    p, o, t, v, f = (
        g(k) or {} for k in ("period", "open", "throughput", "verify", "flow")
    )
    med = lambda d: (d or {}).get("median")  # noqa: E731
    return {
        "ts": ts,
        "project": project,
        "board": board,
        "period_start": p.get("start"),
        "period_end": p.get("end"),
        "days": p.get("days"),
        "open": o.get("total", 0),
        "done": t.get("done", 0),
        "closed": t.get("closed", 0),
        "verify_queue": len(v.get("queue") or []),
        "verify_median": med(v.get("verify_days")),
        "review_median": med(v.get("review_days")),
        "cycle_median": med(t.get("cycle_days")),
        "coverage": v.get("coverage"),
        "weak": len(v.get("weak") or []),
        "tight": len(f.get("tight") or []),
        "late_milestones": len(f.get("late_milestones") or []),
        "overdue": f.get("overdue", 0),
        "stuck": len(f.get("stuck") or []),
        **{k: len(f.get(k) or []) for k in FLAGS},
        "by_epic": o.get("by_epic") or {},
        "by_milestone": g("by_milestone") or {},
        "by_assignee": o.get("by_assignee") or {},
    }


def load_rows(path):
    """Rows of a JSONL file; a missing file is no rows."""
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(x) for x in lines if x.strip()]


def _week_key(row):
    return (row.get("project"), row.get("board"), (row.get("period_end") or "")[:10])


def append_row(path, row):
    """Add `row`, replacing any earlier run for the same project/board/week."""
    # ponytail: rewrite-on-append; append-only + dedupe-on-read past ~10k lines
    rows = [r for r in load_rows(path) if _week_key(r) != _week_key(row)] + [row]
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("".join(json.dumps(r) + "\n" for r in rows))


def weekly(rows, project, weeks=8, board=None):
    """The last `weeks` rows for a project (and board), oldest first."""
    mine = [
        r
        for r in rows
        if r.get("project") == project and (board is None or r.get("board") == board)
    ]
    return sorted(mine, key=lambda r: r.get("period_end") or "")[-weeks:]


# --- rendering ---------------------------------------------------------------


def _n(v):
    return "–" if v is None else v


def _trend(pair):
    prev, now = pair
    arrow = (
        ""
        if prev is None or now is None or prev == now
        else (" ▲" if now > prev else " ▼")
    )
    return f"{_n(now)}{arrow} (prev {_n(prev)})"


def _table(rows, *head):
    if not rows:
        return "_none_"
    body = ["| " + " | ".join(str(_n(c)) for c in r) + " |" for r in rows]
    return "\n".join(["| " + " | ".join(head) + " |", "|" + "---|" * len(head), *body])


def _counts(d):
    return _table(sorted(d.items(), key=lambda kv: (-kv[1], str(kv[0]))), "name", "n")


def milestone_lines(by_ms):
    """Soonest due first, undated last: [(title, due, open, done, added)]."""
    return sorted(
        ((m, v.get("due"), v["open"], v["done"], v["added"]) for m, v in by_ms.items()),
        key=lambda r: (r[1] is None, r[1] or "", r[0]),
    )


def _milestones(by_ms):
    if not by_ms:
        return []
    lines = [
        f"- {m}{f' (due {d})' if d else ''}: {o} open, {dn} done, +{a} added"
        for m, d, o, dn, a in milestone_lines(by_ms)
    ]
    return ["", "### By milestone", *lines]


def _stat_row(label, s):
    return (label, s["median"], s["mean"], s["n"])


def _weak_table(weak, verifier=False):
    """Verified with steps unticked, or minutes after entering Verify."""
    who = ("verifier",) if verifier else ()
    return _table(
        [
            (
                w["iid"],
                w["title"],
                *([w["verifier"]] if verifier else []),
                ", ".join(w["reasons"]),
            )
            for w in weak
        ],
        "iid",
        "title",
        *who,
        "why",
    )


def _tight_table(tight, who=False):
    """Due dates earlier than the finish the person's history expects."""
    return _table(
        [
            (t["iid"], t["title"], *([t["assignee"]] if who else []),
             t["due"], t["expected"], t["basis"])
            for t in tight
        ],
        "iid", "title", *(("assignee",) if who else ()), "due", "expected", "basis",
    )  # fmt: skip


def lower_bound(m):
    """`3 days late`, prefixed `at least` when a card on the chain had no estimate."""
    late = f"{m['days_late']} days late"
    return (
        f"at least {late}, {m['unestimated']} without estimate"
        if m["unestimated"]
        else late
    )


def _late_md(items):
    """Milestones forecast past their due date; no section when none are."""
    if not items:
        return []
    rows = [(m["milestone"], m["due"], m["expected"], lower_bound(m)) for m in items]
    return [
        "### Late milestones",
        _table(rows, "milestone", "due", "expected", "forecast"),
        "",
    ]


def blocker_items(d):
    """Every flag in `d` (a flow or person dict) as one list, `kind` added."""
    return [{"kind": k, **x} for k in FLAGS for x in d.get(k, [])]


def _blockers_md(d, heading):
    """Where the board disagrees with its links; no section when it agrees."""
    items = blocker_items(d)
    if not items:
        return []
    rows = [(x["kind"], f"#{x['iid']} {x['title']}", x["detail"]) for x in items]
    if (n := sum(x["kind"] == "no_milestone" for x in items)) > 5:
        keep = [r for r in rows if r[0] != "no_milestone"]
        rows = keep + [r for r in rows if r[0] == "no_milestone"][:5]
        rows.append(("no_milestone", f"+{n - 5} more", ""))
    return [heading, _table(rows, "kind", "card", "detail"), ""]


def render_weekly_md(rows):
    return _table(
        [
            (
                (r.get("period_end") or "")[:10],
                r.get("done"),
                r.get("open"),
                r.get("verify_queue"),
                r.get("verify_median"),
                r.get("review_median"),
                r.get("cycle_median"),
                None if r.get("coverage") is None else f"{r['coverage']:.0%}",
                r.get("overdue"),
                r.get("stuck"),
            )
            for r in rows
        ],
        "week",
        "done",
        "open",
        "verify q",
        "verify d",
        "review d",
        "cycle d",
        "coverage",
        "overdue",
        "stuck",
    )


def render_team_md(summary, weekly=None):
    p, o, t, v, f, tr = (
        summary[k] for k in ("period", "open", "throughput", "verify", "flow", "trend")
    )
    cov = "–" if v["coverage"] is None else f"{v['coverage']:.0%}"
    parts = [
        f"# Team — {p['days']} days to {p['end'][:10]}",
        "",
        f"**Open {o['total']}** ({o['unassigned']} unassigned) · "
        f"opened {t['opened']} · done {_trend(tr['done'])} · closed {t['closed']}",
        "",
        "## Open",
        "",
        "### By column",
        _counts(o["by_column"]),
        "",
        "### By epic",
        _counts(o["by_epic"]),
        "",
        "### By story",
        _counts(o["by_story"]),
        *_milestones(summary.get("by_milestone")),
        "",
        "### By type",
        _counts(o["by_type"]),
        "",
        "### By assignee",
        _counts(o["by_assignee"]),
        "",
        "## Done this period",
        "",
        "### By assignee",
        _counts(t["done_by"]["assignee"]),
        "",
        "### By epic",
        _counts(t["done_by"]["epic"]),
        "",
        "### By story",
        _counts(t["done_by"]["story"]),
        "",
        "## Time",
        _table(
            [
                _stat_row("cycle (created → done)", t["cycle_days"]),
                _stat_row("in Verify", v["verify_days"]),
                _stat_row("in Review", v["review_days"]),
            ],
            "days",
            "median",
            "mean",
            "n",
        ),
        "",
        f"Trend: cycle median {_trend(tr['cycle_median'])}, "
        f"verify median {_trend(tr['verify_median'])}",
        "",
        "## Verification",
        "",
        f"Verified {v['verified']}, failed {v['failed']}, coverage {cov} · "
        f"queue {len(v['queue'])}, oldest {_n(v['oldest_days'])} days",
        "",
        "### Verifiers",
        _counts(v["verifiers"]),
        "",
        "### Weak verdicts",
        _weak_table(v.get("weak", []), verifier=True),
        "",
        "### Queue",
        _table(
            [(q["iid"], q["title"], q["assignee"], q["days"]) for q in v["queue"]],
            "iid",
            "title",
            "assignee",
            "days",
        ),
        "",
        "## Flow",
        "",
        f"Overdue {f['overdue']} · stale {f['stale']} · re-verify {f['reverify']} · "
        f"multi-scope {', '.join(f'#{i}' for i in f['multi_scope']) or '–'}",
        "",
        "### Stuck",
        _table(f["stuck"], "iid", "column", "days"),
        "",
        "### Tight dates",
        _tight_table(f.get("tight", []), who=True),
        "",
        *_late_md(f.get("late_milestones", [])),
        *_blockers_md(f, "### Blockers"),
        "### WIP",
        _counts(f["wip"]),
        "",
        "### Questions waiting",
        _table(
            [(q["iid"], q["author"], q["text"], q["days"]) for q in f["questions"]],
            "iid",
            "asked by",
            "question",
            "days",
        ),
        "",
    ]
    md = "\n".join(parts)
    if weekly is not None:
        md += "\n## 8-week trend\n" + render_weekly_md(weekly)
    return md


def render_person_md(person, summary, username):
    """The person's section, a rule, then the team summary."""
    parts = [
        f"# {username}",
        "",
        f"Done {len(person['done'])} · verified {person['verified']} · "
        f"failed {person['failed']}",
        "",
        "## Your verify queue",
        _table(
            [(q["iid"], q["title"], q["days"]) for q in person["verify_queue"]],
            "iid",
            "title",
            "days",
        ),
        "",
        "## Your weak verdicts",
        _weak_table(person.get("weak", [])),
        "",
        "## Overdue",
        _table(
            [(o["iid"], o["title"], o["due"]) for o in person["overdue"]],
            "iid",
            "title",
            "due",
        ),
        "",
        "## Tight dates",
        _tight_table(person.get("tight", [])),
        "",
        *_blockers_md(person, "## Your blockers"),
        "## Open by column",
        _counts(person["open_by_column"]),
        "",
        "## Done this period",
        _table([(d["iid"], d["title"]) for d in person["done"]], "iid", "title"),
        "",
        "## Questions waiting on you",
        _table(
            [
                (q["iid"], q["author"], q["text"], q["days"])
                for q in person["questions"]
            ],
            "iid",
            "asked by",
            "question",
            "days",
        ),
        "",
        "---",
        "",
        render_team_md(summary),
    ]
    return "\n".join(parts)


def eml(to, subject, body, sender=None, now=None, html=None):
    """An RFC 5322 message the lead can drop into a mail client.

    With `html`, multipart/alternative: the text part stays the primary body.
    """
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = format_datetime(now or datetime.now(UTC))
    if sender:
        msg["From"] = sender
    msg.set_content(body, charset="utf-8")
    if html is not None:
        msg.add_alternative(html, subtype="html", charset="utf-8")
    return msg.as_string()


def slug(project):
    """`grp/proj` -> `grp-proj`, safe as a directory name."""
    return project.replace("/", "-")
