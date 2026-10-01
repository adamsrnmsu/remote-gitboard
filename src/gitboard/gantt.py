"""Gantt charts for the digest: hacker-dark panels drawn as table cells.

Outlook-safe for the same reasons as `mail.py`, whose primitives it uses: a
bar is a run of `td`s, never an image or SVG, every `td` carries `bgcolor`
and every text run a colour. A track is painted pixel by pixel and then
run-length encoded into cells, so clipping, the 4px minimum and the today /
due markers need no special cases.

A card's bar starts when it first entered a column (`estimate.started`; a
Backlog card starts today) and ends when it finished, else at its due date,
else at the finish its assignee's history expects, else today.
"""

from datetime import date, timedelta
from html import escape

from gitboard import estimate, mail, stats
from gitboard.mail import MONO, PX, td

HACK = {
    "surface": "#0a0f0a",
    "track": "#121a12",
    "ink": "#b6f5c0",
    "muted": "#7fa886",
    "marker": "#e8ffe8",
}
# validated with the dataviz script against HACK["surface"]: CVD at the 6.1
# floor, legal because every row also names its status in words
STATUS = {
    "done": "#5a8ff0",
    "due": "#21a650",
    "est": "#c27f12",
    "late": "#e04f9a",
    "undated": HACK["muted"],
}
LEGEND = {
    "done": "done",
    "due": "on track",
    "est": "forecast",
    "late": "late",
    "undated": "no date",
}
LABEL_W, TRACK_W, TAG_W = 180, 330, 90  # one 600px row
LOOKBACK = timedelta(days=42)
ROWS = 12


def bars(history, columns, now, cfg):
    """One bar per card worth placing in time: done in the last LOOKBACK,
    on the board, or in Backlog with a due date or an estimate."""
    today = now.date()
    pool = estimate.samples(history)
    out = []
    for i in history:
        on_board = any(c in i["labels"] for c in columns)
        done = stats.done_at(i)
        start = estimate.started(i).date() if on_board or done else today
        due = date.fromisoformat(i["due_date"]) if i.get("due_date") else None
        if done is not None:
            if done.date() < today - LOOKBACK:
                continue
            status, end, tag = "done", done.date(), f"done {done:%m-%d}"
        elif due and due < today:
            status, end, tag = "late", today, f"late {due:%m-%d}"
        elif due:
            status, end, tag = "due", due, f"due {due:%m-%d}"
        elif (
            i["assignee"]
            and not estimate.SKIP & set(i["labels"])
            and (
                est := estimate.estimate(
                    i["assignee"], i["labels"], pool, cfg["method"], cfg["min_samples"]
                )
            )
        ):
            end = estimate._expected(i, est, columns, today)
            status, tag = "est", f"~{end:%m-%d} est"
        elif on_board:
            status, end, tag = "undated", today, "no date"
        else:
            continue  # Backlog with nothing to place it by
        out.append(
            {"iid": i["iid"], "title": i["title"], "url": i.get("web_url"),
             "assignee": i["assignee"], "milestone": i.get("milestone"),
             "milestone_due": i.get("milestone_due"),
             "epic": stats.scoped(i["labels"], "epic"), "start": min(start, end),
             "end": end, "status": status, "tag": tag}
        )  # fmt: skip
    return sorted(out, key=lambda b: (b["start"], b["end"], str(b["iid"])))


def _dues(rows, milestones):
    """Milestone title -> due ISO string (or None), active list first."""
    due = {m["title"]: m.get("due_date") for m in milestones}
    for b in rows:
        if b["milestone"]:
            due[b["milestone"]] = due.get(b["milestone"]) or b["milestone_due"]
    return due


def current_milestone(rows, milestones=()):
    """The earliest-due milestone that still has open cards; with no dates,
    the one holding the most open cards. None when none has open cards."""
    due = _dues(rows, milestones)
    open_n = {}
    for b in rows:
        if m := b["milestone"]:
            open_n[m] = open_n.get(m, 0) + (b["status"] != "done")
    live = [m for m, n in open_n.items() if n]
    if not live:
        return None
    return min(live, key=lambda m: (due[m] is None, due[m] or "", -open_n[m], m))


def groups(rows, milestones, today):
    """The project view: one row per milestone (else per `epic::`), its bar
    from the first card's start to the later of its due date and last end,
    the done share painted from the left. Late: a late card, or open cards
    and the due date passed, or a card's end falls after it."""
    by_ms = any(b["milestone"] for b in rows)
    due = _dues(rows, milestones)
    by = {}
    for b in rows:
        if name := b["milestone"] if by_ms else b["epic"]:
            by.setdefault(name, []).append(b)
    out = []
    for name, cards in by.items():
        d = due.get(name) if by_ms else None
        d = date.fromisoformat(d) if d else None
        n, done = len(cards), sum(b["status"] == "done" for b in cards)
        last = max(b["end"] for b in cards)
        late = any(b["status"] == "late" for b in cards) or (
            done < n and d is not None and (d < today or last > d)
        )
        out.append(
            {"title": name, "start": min(b["start"] for b in cards),
             "end": max(last, d or last), "due": d, "done": done / n,
             "status": "late" if late else "done" if done == n else "due",
             "tag": f"{done}/{n} done"}
        )  # fmt: skip
    return sorted(
        out, key=lambda g: (g["due"] is None, g["due"] or g["end"], g["title"])
    )


# --- drawing ------------------------------------------------------------------


def _text(text, color=None, size=12, style=""):
    color = color or HACK["ink"]
    css = f"font-family:{MONO};font-size:{size}px;color:{color};{style}"
    return f'<span style="{css}">{escape(str(text))}</span>'


def _cell(content, width=None, style="", **attrs):
    if width is not None:
        attrs["width"] = width
    return td(content, HACK["surface"], style=style, **attrs)


def _table(rows, width=None):
    w = f' width="{width}"' if width else ""
    return (
        f'<table cellpadding="0" cellspacing="0" border="0"{w} role="presentation">'
        f"{rows}</table>"
    )


def _cut(text, n=22, ell="…"):
    return text if len(text) <= n else text[: n - 1] + ell


def _label(row):
    head = f"#{row['iid']} " if row.get("iid") is not None else ""
    text = escape(_cut(f"{head}{row['title']}"))
    css = f"font-family:{MONO};font-size:12px;color:{HACK['ink']};text-decoration:none"
    url = row.get("url") or ""
    if url.startswith(("https://", "http://")):
        return f'<a href="{escape(url, quote=True)}" style="{css}">{text}</a>'
    return f'<span style="{css}">{text}</span>'


def paint(row, lo, hi, marks):
    """[(colour, px)] runs across TRACK_W: the bar (done share first, at
    least 4px, clipped to the axis) and a 2px marker per date in `marks`."""
    days = (hi - lo).days

    def px(d):
        return round((d - lo).days / days * TRACK_W)

    cells = [HACK["track"]] * TRACK_W
    b = min(TRACK_W, max(4, px(row["end"] + timedelta(days=1))))
    a = min(max(0, px(row["start"])), b - 4)
    split = a + round((b - a) * row.get("done", 0))
    for x in range(a, b):
        cells[x] = STATUS["done"] if x < split else STATUS[row["status"]]
    for d in marks:
        if d and lo <= d < hi:
            x = min(px(d), TRACK_W - 2)
            cells[x : x + 2] = [HACK["marker"]] * 2
    runs = []
    for c in cells:
        if runs and runs[-1][0] == c:
            runs[-1][1] += 1
        else:
            runs.append([c, 1])
    return [tuple(r) for r in runs]


def _track(runs):
    cells = "".join(td("&nbsp;", c, style=PX, width=n, height=10) for c, n in runs)
    return _table(f"<tr>{cells}</tr>", TRACK_W)


def _legend(statuses, note):
    cells = "".join(
        td("&nbsp;", STATUS[s], style=PX, width=10, height=10)
        + _cell(_text(LEGEND[s], HACK["muted"], 11), style="padding:0 12px 0 6px")
        for s in LEGEND
        if s in statuses
    )
    return _table(f"<tr>{cells}{_cell(_text(note, HACK['muted'], 11))}</tr>")


def _span(shown, today, due):
    """The axis [lo, hi) shared by the HTML and the text chart."""
    lo = min(max(min(r["start"] for r in shown), today - LOOKBACK), today)
    ends = [r["end"] for r in shown] + [today] + ([due] if due else [])
    return lo, max(ends) + timedelta(days=1)


def chart(command, rows, today, due=None, cap=ROWS, why=""):
    """One panel: `$ command`, a date axis, a row per bar, a legend. The
    axis starts at the later of the first start and LOOKBACK ago; a bar
    that began earlier is clipped and marked ◂. Rows past `cap` fold into
    `+n more`. Returns a `<tr>` for `mail.zone`, or "" with no rows."""
    if not rows:
        return ""
    shown, rest = rows[:cap], len(rows) - (cap or len(rows))
    lo, hi = _span(shown, today, due)
    pad = "padding:3px 0 3px 10px"
    comment = f"# {why + ' · ' if why else ''}{len(rows)} rows"
    head = _cell(
        _text(f"$ {command}", style="font-weight:bold")
        + "&nbsp;&nbsp;"
        + _text(comment, HACK["muted"], 11),
        style="padding:10px 10px 6px",
        colspan=3,
    )
    axis = (
        _cell("&nbsp;", LABEL_W)
        + _cell(
            _table(
                "<tr>"
                + _cell(_text(f"{lo:%m-%d}", HACK["muted"], 10))
                + _cell(_text(f"{hi:%m-%d}", HACK["muted"], 10), align="right")
                + "</tr>",
                TRACK_W,
            ),
            TRACK_W,
        )
        + _cell("&nbsp;", TAG_W)
    )
    body = []
    for r in shown:
        clip = _text("◂ ", HACK["muted"], 10) if r["start"] < lo else ""
        runs = paint(r, lo, hi, [today, due, r.get("due")])
        body.append(
            "<tr>"
            + _cell(clip + _label(r), LABEL_W, style=pad)
            + _cell(_track(runs), TRACK_W, style="padding:3px 0")
            + _cell(_text(r["tag"], HACK["muted"], 11), TAG_W, style="padding:3px 8px")
            + "</tr>"
        )
    if rest > 0:
        more = _text(f"+{rest} more in gantt.html", HACK["muted"], 11)
        body.append(f"<tr>{_cell(more, colspan=3, style=pad)}</tr>")
    statuses = {r["status"] for r in shown} | {"done" for r in shown if r.get("done")}
    note = "│ today" + (f" · │ due {due:%m-%d}" if due else "")
    if any(r.get("due") for r in shown):
        note += " · │ milestone due"
    legend = _cell(_legend(statuses, note), colspan=3, style="padding:6px 10px 10px")
    s = HACK["surface"]
    panel = (
        '<table cellpadding="0" cellspacing="0" border="0" width="600" '
        f'role="presentation" bgcolor="{s}" style="background:{s}">'
        f"<tr>{head}</tr><tr>{axis}</tr>{''.join(body)}<tr>{legend}</tr></table>"
    )
    return f"<tr>{td(panel, style='padding:12px 0 0')}</tr>"


TEXT_W = 30  # columns across the axis
LINE_MAX = 100


def _md(d):
    return f"{d:%b} {d.day}"


def chart_text(command, rows, today, due=None, cap=ROWS, why=""):
    """ASCII twin of `chart` for text-only clients: same axis, one line per
    row, `#12 title  |..###...|  May 4 - May 20  tag`. "" with no rows."""
    if not rows:
        return ""
    shown, rest = rows[:cap], len(rows) - (cap or len(rows))
    lo, hi = _span(shown, today, due)
    days = (hi - lo).days

    def col(d):
        return round((d - lo).days / days * TEXT_W)

    out = [f"$ {command}  # {why + ' - ' if why else ''}{len(rows)} rows",
           f"{'':26}  {_md(lo)} .. {_md(hi - timedelta(days=1))}"]  # fmt: skip
    for r in shown:
        b = min(TEXT_W, max(1, col(r["end"] + timedelta(days=1))))
        a = min(max(0, col(r["start"])), b - 1)
        bar = "." * a + "#" * (b - a) + "." * (TEXT_W - b)
        head = f"#{r['iid']} " if r.get("iid") is not None else ""
        label = _cut(f"{head}{r['title']}", 26, "~")
        label = label.encode("ascii", "replace").decode()
        span = f"{_md(max(r['start'], lo))} - {_md(r['end'])}"
        out.append(f"{label:<26}  |{bar}|  {span}  {r['tag']}"[:LINE_MAX])
    if rest > 0:
        out.append(f"+{rest} more in gantt.html")
    return "\n".join(out)


# --- the three views ----------------------------------------------------------


def person_chart(rows, who, today, cap=ROWS, draw=chart):
    mine = [b for b in rows if b["assignee"] == who]
    return draw(f"gantt --who {who}", mine, today, cap=cap)


def milestone_chart(rows, milestones, today, cap=ROWS, draw=chart):
    """The current milestone's cards; the header says why it is current."""
    m = current_milestone(rows, milestones)
    if m is None:
        return ""
    due = _dues(rows, milestones)[m]
    due = date.fromisoformat(due) if due else None
    mine = [b for b in rows if b["milestone"] == m]
    why = "current: soonest due, still open" if due else "current: most open cards"
    return draw(f"gantt --milestone '{m}'", mine, today, due, cap, why)


def project_chart(rows, milestones, today, draw=chart):
    gs = groups(rows, milestones, today)
    by = "milestones" if any(b["milestone"] for b in rows) else "epics"
    return draw("gantt --project", gs, today, cap=len(gs), why=f"by {by}")


def team_blocks(rows, milestones, today, people, cap=ROWS, draw=chart):
    """Team mail and gantt.html (cap None): project, current milestone,
    every person."""
    blocks = [project_chart(rows, milestones, today, draw)]
    blocks.append(milestone_chart(rows, milestones, today, cap, draw))
    blocks += [person_chart(rows, who, today, cap, draw) for who in sorted(people)]
    return [b for b in blocks if b]


def person_blocks(rows, milestones, today, who, draw=chart):
    """A person's mail: their own bars, then the milestone and the project."""
    blocks = [
        person_chart(rows, who, today, draw=draw),
        milestone_chart(rows, milestones, today, draw=draw),
        project_chart(rows, milestones, today, draw),
    ]
    return [b for b in blocks if b]


def person_text(rows, milestones, today, who):
    """The person's charts as ASCII, fenced for markdown; "" when none."""
    blocks = person_blocks(rows, milestones, today, who, chart_text)
    if not blocks:
        return ""
    return (
        "\n## Timeline\n\n" + "\n\n".join(f"```text\n{b}\n```" for b in blocks) + "\n"
    )


def render_page(title, blocks):
    """gantt.html: every chart uncapped, in the digest's dark page."""
    return mail.page(title, [mail.zone("TIMELINE", title, STATUS["due"], blocks)])
