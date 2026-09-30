"""Per-person estimates from the board's own history. Pure, stdlib only.

A sample is a finished card's active days; an estimate is a percentile of
the narrowest bucket — person + type, person, team + type, team — that holds
enough samples. `suggest` stages due dates into a spec; `tight` lists the
due dates the history contradicts; `late_milestones` runs a milestone's
critical chain against its due date. Imports `stats`; `stats` must not
import this back.
"""

from datetime import date, timedelta
from math import ceil

from gitboard import graph, stats
from gitboard.apply import SpecError

METHODS = {"median": 0.5, "p85": 0.85}
DEFAULTS = {"suggest_due": True, "method": "p85", "min_samples": 5}
HISTORY_DAYS = 90
SKIP = {stats.VERIFY, stats.DONE, stats.FAILED}


def config(spec):
    """The spec's `estimates:` over DEFAULTS; a bad value is a SpecError."""
    raw = spec.get("estimates")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SpecError("estimates: expected a mapping")
    cfg = {**DEFAULTS, **raw}
    if cfg["method"] not in METHODS:
        raise SpecError(
            f"estimates.method: {cfg['method']!r} is not one of {', '.join(METHODS)}"
        )
    if not isinstance(cfg["min_samples"], int) or cfg["min_samples"] < 1:
        raise SpecError("estimates.min_samples: expected a whole number, 1 or more")
    return cfg


def started(issue):
    """When the card first entered a column; Backlog wait is nobody's pace."""
    adds = [stats.parse_ts(t) for t, act, _ in issue["transitions"] if act == "add"]
    return min(adds) if adds else stats.parse_ts(issue["created_at"])


def samples(history):
    """[(assignee, type, active_days)] for every finished card."""
    out = []
    for i in history:
        done = stats.done_at(i)
        if done is not None:
            days = max(0.0, (done - started(i)) / stats.DAY)
            out.append((i["assignee"], stats.scoped(i["labels"], "type"), days))
    return out


def estimate(assignee, labels, pool, method="p85", min_samples=5):
    """{days, n, basis} from the narrowest bucket with enough samples, or None.

    Nearest-rank percentile, so the figure is a duration that happened;
    rounded up to whole days, at least 1.
    """
    kind = stats.scoped(labels, "type")
    ladder = [
        (assignee and kind, f"{assignee}, type::{kind}",
         lambda s: s[0] == assignee and s[1] == kind),
        (assignee, f"{assignee}", lambda s: s[0] == assignee),
        (kind, f"team, type::{kind}", lambda s: s[1] == kind),
        (True, "team", lambda s: True),
    ]  # fmt: skip
    for usable, name, keep in ladder:
        days = sorted(s[2] for s in pool if keep(s)) if usable else []
        if len(days) >= min_samples:
            pick = days[ceil(METHODS[method] * len(days)) - 1]
            return {
                "days": max(1, ceil(pick)),
                "n": len(days),
                "basis": f"{method} of {len(days)} cards: {name}",
            }
    return None


def suggest(spec, history, today):
    """Stage `due_date = today + estimate` on assigned, undated, open work.

    Mutates `spec` unless `estimates.suggest_due` is false; either way the
    rows say what it would set and why. A date that exists is never touched.
    """
    cfg = config(spec)
    pool = samples(history)
    rows = []
    for issue in spec["issues"]:
        labels = issue.get("labels") or []
        who = issue.get("assignee")
        if issue.get("due_date") or not who or SKIP & set(labels):
            continue
        est = estimate(who, labels, pool, cfg["method"], cfg["min_samples"])
        if est is None:
            continue
        # ponytail: today + days ignores how many cards the person already
        # holds; queue-aware dating if the dates prove optimistic
        due = (today + timedelta(days=est["days"])).isoformat()
        rows.append(
            {"iid": issue.get("iid"), "title": issue["title"], "assignee": who,
             "due": due, **est}
        )  # fmt: skip
        if cfg["suggest_due"]:
            issue["due_date"] = due
    return {"rows": rows, "changed": cfg["suggest_due"] and bool(rows)}


def _expected(issue, est, columns, today):
    """A card on the board finishes `started + days` (never earlier than
    today); a Backlog card `today + days`."""
    span = timedelta(days=est["days"])
    if any(c in issue["labels"] for c in columns):
        return max((started(issue) + span).date(), today)
    return today + span


def tight(history, columns, now, cfg):
    """Open cards whose due date falls before the finish the history expects.

    Past due dates are `overdue`'s business; the finish is `_expected`.
    """
    pool = samples(history)
    today = now.date()
    out = []
    for i in history:
        due, who = i.get("due_date"), i["assignee"]
        if i["state"] != "opened" or not who or not due or due < today.isoformat():
            continue
        if SKIP & set(i["labels"]):
            continue
        est = estimate(who, i["labels"], pool, cfg["method"], cfg["min_samples"])
        if est is None:
            continue
        expected = _expected(i, est, columns, today)
        if expected.isoformat() > due:
            out.append(
                {"iid": i["iid"], "title": i["title"], "assignee": who, "due": due,
                 "expected": expected.isoformat(), "basis": est["basis"],
                 "url": i["web_url"]}
            )  # fmt: skip
    return sorted(out, key=lambda t: (t["due"], t["iid"]))


def late_milestones(history, columns, now, cfg):
    """Milestones due today or later whose critical chain is forecast past it.

    Forecast = today + the remaining days (`_expected`) of the chain's open
    cards, one after another. A card with no estimate adds nothing, so the
    figure is a lower bound and `unestimated` counts those cards. Cards in
    Verify, Done or Failed have no work left. Past-due milestones are
    `overdue`'s business.
    """
    pool = samples(history)
    today = now.date()
    g = graph.build(history)
    by_key = {graph._key(c): c for c in history}
    out = []
    for mk, path in g["critical"].items():
        title, due = g["nodes"][mk]["title"], g["nodes"][mk]["due_date"]
        if not due or due < today.isoformat():
            continue
        chain = [by_key.get(k) for k in path[:-1] if g["nodes"][k]["open"]]
        chain = [i for i in chain if i is None or not SKIP & set(i["labels"])]
        days = unestimated = 0
        for i in chain:
            est = i and estimate(
                i["assignee"], i["labels"], pool, cfg["method"], cfg["min_samples"]
            )
            if est:
                days += (_expected(i, est, columns, today) - today).days
            else:
                unestimated += 1
        # ponytail: the chain is one card after another, so parallel work on
        # the milestone is ignored, a ceiling; forecast from the whole
        # milestone's open work and its owners if that proves too gloomy.
        expected = today + timedelta(days=days)
        if expected.isoformat() > due:
            out.append(
                {"milestone": title, "due": due, "expected": expected.isoformat(),
                 "days_late": (expected - date.fromisoformat(due)).days,
                 "cards": len(chain), "unestimated": unestimated}
            )  # fmt: skip
    return sorted(out, key=lambda m: (m["due"], m["milestone"]))
