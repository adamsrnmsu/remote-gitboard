"""Summaries (from test_stats' fixtures) that exercise every branch of the team
report; the golden file holds render_team_md's output for them."""

from test_stats import (
    BLOCKED_COLUMNS,
    COLUMNS,
    END,
    LATE,
    NOW,
    START,
    TIGHT,
    _verified,
    blockers,
    history,
    issue,
    row,
)

from gitboard import stats


def _s(h=None, cols=COLUMNS):
    return stats.summarise(history() if h is None else h, cols, START, END, NOW)


def cases():
    """name -> (summary, weekly)."""
    flow = _s()
    flow["flow"]["tight"] = [TIGHT]
    flow["flow"]["late_milestones"] = [
        LATE,
        {**LATE, "milestone": "Rc", "unestimated": 0},
    ]
    beta = {"milestone": "Beta", "milestone_due": "2026-11-01"}
    ms = _s(
        [
            {**issue(1, created=12), **beta},
            {**issue(3, created=3, closed=14), **beta},
            {
                **issue(4, created=13),
                "milestone": "Alpha",
                "milestone_due": "2026-10-01",
            },
            {**issue(5, created=13), "milestone": "Undated"},
        ]
    )
    weeks = [row(done=2, coverage=0.5), row(end="2026-09-23T12:00:00+00:00")]
    return {
        "plain": (_s(), None),
        "weekly": (_s(), weeks),
        "weekly_none_logged": (_s(), []),
        "empty": (_s([]), None),
        "empty_weekly": (_s([]), weeks),
        "blockers": (_s(blockers(), BLOCKED_COLUMNS), None),
        "late_tight": (flow, None),
        "by_milestone": (ms, weeks),
        "weak": (_s([_verified(1, tasks=(0, 2))]), None),
    }
