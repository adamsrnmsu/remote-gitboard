"""HTML weekly digest: dark "Ice" look, charts as table cells, Outlook-safe.

Stdlib only. Outlook on Windows renders with Word, so: tables and inline
styles, px widths, no images, no SVG. Every `td` carries `bgcolor` + an
inline background and every text run an explicit colour, which is what stops
Outlook's dark-mode partial invert. The browser copy (`svg=True`) adds one
inline SVG burndown on top.
"""

from datetime import date
from html import escape

from gitboard import stats

THEME = {
    "surface": "#14171c",
    "panel": "#1e2329",
    "text": "#ffffff",
    "muted": "#9aa4b2",
    "accent": "#4da3ff",
    "warn": "#c98500",
    "danger": "#e66767",
    "good": "#199e70",
}
COLUMN_COLORS = {
    "Doing": "#3987e5",
    "Review": "#d55181",
    "Verify": "#c98500",
    "Done": "#199e70",
    "Failed": "#d03b3b",
    "Backlog": "#7f8ea3",
}
EXTRA_COLORS = ["#d95926", "#9085e9"]
MONO = "Consolas,'Courier New',monospace"
SANS = "'Segoe UI',Arial,sans-serif"
BAR_W = 380
CAP = 5
PX = "font-size:1px;line-height:1px;mso-line-height-rule:exactly"


# --- primitives ---------------------------------------------------------------


def td(content, bg=None, style="", **attrs):
    """A cell that always carries bgcolor + inline background."""
    bg = bg or THEME["surface"]
    extra = "".join(f' {k}="{escape(str(v), quote=True)}"' for k, v in attrs.items())
    return f'<td bgcolor="{bg}" style="background:{bg};{style}"{extra}>{content}</td>'


def span(text, color=None, font=SANS, size=13, style=""):
    color = color or THEME["text"]
    css = f"font-family:{font};font-size:{size}px;color:{color};{style}"
    return f'<span style="{css}">{escape(str(text))}</span>'


def table(rows, width=600, **attrs):
    extra = "".join(f' {k}="{escape(str(v), quote=True)}"' for k, v in attrs.items())
    return (
        f'<table cellpadding="0" cellspacing="0" border="0" width="{width}" '
        f'role="presentation"{extra}>{rows}</table>'
    )


def link(item):
    """`#iid title` as an accent link when the item has a url, else plain."""
    label = escape(f"#{item.get('iid', '?')} {item.get('title', '')}".strip())
    css = f"font-family:{SANS};font-size:13px;"
    if item.get("url"):
        href = escape(item["url"], quote=True)
        return (
            f'<a href="{href}" style="{css}color:{THEME["accent"]};'
            f'text-decoration:none">{label}</a>'
        )
    return f'<span style="{css}color:{THEME["text"]}">{label}</span>'


def column_color(name, columns=()):
    """Series colour for a column; unknown ones take EXTRA_COLORS in board order."""
    name = name.split("+")[0]
    if name in COLUMN_COLORS:
        return COLUMN_COLORS[name]
    extra = [c for c in columns if c not in COLUMN_COLORS]
    k = extra.index(name) if name in extra else len(EXTRA_COLORS)
    return (EXTRA_COLORS + [COLUMN_COLORS["Backlog"]])[min(k, len(EXTRA_COLORS))]


def _cap(items):
    return list(items)[:CAP], max(0, len(items) - CAP)


def _more(n):
    if not n:
        return ""
    cell = td(span(f"+{n} more", THEME["muted"], size=12), style="padding:4px 0")
    return f"<tr>{cell}</tr>"


def _none(text="none"):
    return f"<tr>{td(span(text, THEME['muted'], size=12))}</tr>"


# --- blocks -------------------------------------------------------------------


def bar_row(label, value, max_value, color):
    """label · bar (only when value > 0) · value."""
    cells = [
        td(
            span(label, THEME["muted"], size=12),
            width=140,
            style="padding:3px 8px 3px 0",
        )
    ]
    if value > 0:
        w = max(4, round(value / max(1, max_value) * BAR_W))
        cells.append(td("&nbsp;", color, style=PX, width=w, height=12))
    cells.append(td(span(value, font=MONO, size=12), style="padding:0 0 0 8px"))
    return "<tr>" + "".join(cells) + "</tr>"


def bars(items, color_for):
    """Horizontal bars for [(label, value)], capped at CAP rows."""
    items, rest = _cap(items)
    if not items:
        return table(_none())
    col = color_for if callable(color_for) else (lambda _: color_for)
    mx = max(v for _, v in items)
    rows = "".join(bar_row(lb, v, mx, col(lb)) for lb, v in items)
    return table(rows + _more(rest))


def _weekday(label):
    try:
        return date.fromisoformat(str(label)).strftime("%a")[:2]
    except ValueError:
        return str(label)


def column_chart(series, key, color, height=80, label_key="date"):
    """One nested 1-col table per day: spacer td over a filled td."""
    if not series:
        return table(_none("no data"))
    vals = [int(p.get(key) or 0) for p in series]
    mx = max(vals)
    peak = vals.index(mx)
    show = {0, len(vals) - 1, peak}
    w = max(8, 600 // len(vals))
    tops, cols, days = [], [], []
    for k, (p, v) in enumerate(zip(series, vals, strict=True)):
        h = 0 if v == 0 else max(2, round(v / max(1, mx) * height))
        label = span(v, THEME["muted"], MONO, 10) if k in show else "&nbsp;"
        tops.append(td(label, width=w, align="center", style="padding:0 0 4px"))
        inner = td("&nbsp;", style=PX, height=height - h)
        if h:
            inner += "</tr><tr>" + td("&nbsp;", color, style=PX, height=h)
        col = table(f"<tr>{inner}</tr>", width="100%")
        # 2px surface gap each side: adjacent fills must not read as one block
        cols.append(td(col, width=w, valign="bottom", style="padding:0 2px"))
        days.append(
            td(
                span(_weekday(p.get(label_key, "")), THEME["muted"], MONO, 10),
                width=w,
                align="center",
                style="padding:4px 0 0",
            )
        )
    rows = "".join(f"<tr>{''.join(r)}</tr>" for r in (tops, cols, days))
    return table(rows)


def stat_tile(label, value, delta=None, good=None):
    """A panel cell: big number, small label, optional delta."""
    body = (
        f"<div>{span(value, font=MONO, size=28)}</div>"
        f"<div>{span(label, THEME['muted'], size=11)}</div>"
    )
    if delta:
        color = THEME["good"] if good else THEME["muted"]
        body += f"<div>{span(delta, color, size=11)}</div>"
    return td(body, THEME["panel"], width=180, align="center", style="padding:12px")


def tiles(*cells):
    gap = td("&nbsp;", width=8, style=PX)
    return table("<tr>" + gap.join(cells) + "</tr>")


def moves_block(moves):
    """Numbered rows: accent number tile · verb + linked card · age."""
    if not moves:
        return table(_none("Inbox zero. Pick from the Verify queue below."))
    rows = []
    for k, m in enumerate(moves, 1):
        num = td(
            span(k, THEME["surface"], MONO, 16),
            THEME["accent"],
            width=32,
            height=32,
            align="center",
        )
        text = f"{span(m.get('verb', ''))} {link(m)}"
        age = td(span(m.get("age", ""), THEME["muted"], MONO, 12), align="right")
        body = td(text, style="padding:6px 12px")
        rows.append(f"<tr>{num}{body}{age}</tr>")
    return table("".join(rows))


def _rows(items, tag, age):
    """Linked rows `tag · #iid title · age`, capped, for queues and questions."""
    items, rest = _cap(items)
    if not items:
        return table(_none())
    rows = []
    for it in items:
        cells = [
            td(span(tag, THEME["muted"], MONO, 11), width=64, style="padding:4px 0"),
            td(link(it), style="padding:4px 8px"),
            td(span(age(it), THEME["muted"], MONO, 12), align="right"),
        ]
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return table("".join(rows) + _more(rest))


def _days(it):
    return f"{it.get('days', '')} d"


def section(title, inner):
    """Heading row + body row, for the inner 600 table."""
    head = span(
        title,
        THEME["accent"],
        size=14,
        style="text-transform:uppercase;letter-spacing:1px;font-weight:bold",
    )
    return f"<tr>{td(head, style='padding:24px 0 8px')}</tr><tr>{td(inner)}</tr>"


def sub(title):
    return (
        f"<div style='padding:8px 0 2px'>{span(title, THEME['muted'], size=12)}</div>"
    )


def page(title, blocks, headers=None):
    """The Outlook skeleton: outer 100% table centring an inner 600 table."""
    s = THEME["surface"]
    top = ""
    if headers:
        pad = "padding:4px 8px"
        rows = "".join(
            "<tr>"
            + td(span(k, THEME["muted"], MONO, 12), THEME["panel"], width=80, style=pad)
            + td(span(v, font=MONO, size=12), THEME["panel"], style=pad)
            + "</tr>"
            for k, v in headers.items()
        )
        top = f"<tr>{td(table(rows), style='padding:0 0 16px')}</tr>"
    inner = table(top + "".join(blocks))
    outer = table(
        f"<tr>{td(inner, align='center', style='padding:24px 12px')}</tr>",
        width="100%",
        bgcolor=s,
        style=f"background:{s}",
    )
    return (
        "<!DOCTYPE html>\n"
        '<html xmlns:o="urn:schemas-microsoft-com:office:office" lang="en">\n'
        '<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width">\n'
        '<meta name="color-scheme" content="dark">\n'
        '<meta name="supported-color-schemes" content="dark">\n'
        f"<title>{escape(title)}</title>\n"
        "<!--[if mso]><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96"
        "</o:PixelsPerInch></o:OfficeDocumentSettings></xml><![endif]-->\n"
        "</head>\n"
        f'<body bgcolor="{s}" style="margin:0;padding:0;background:{s};'
        f'color:{THEME["text"]}">\n{outer}\n</body>\n</html>\n'
    )


def svg_burndown(series, key="open", width=600, height=120):
    """Inline polyline with min/max gridlines; browser copy only."""
    vals = [int(p.get(key) or 0) for p in series]
    if not vals:
        return ""
    lo, hi, pad = min(vals), max(vals), 24
    n, w, h = len(vals), width - 2 * pad, height - 2 * pad
    xs = [pad + w * k / max(1, n - 1) for k in range(n)]
    ys = [height - pad - (v - lo) / max(1, hi - lo) * h for v in vals]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True))
    m, a = THEME["muted"], THEME["accent"]
    font = f'font-family="{MONO}" font-size="10" fill="{m}"'

    def grid(v, y):
        return (
            f'<line x1="{pad}" x2="{width - pad}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="{m}" stroke-dasharray="2,3"/>'
            f'<text x="0" y="{y + 3:.1f}" {font}>{v}</text>'
        )

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        + grid(hi, ys[vals.index(hi)])
        + grid(lo, ys[vals.index(lo)])
        + f'<polyline fill="none" stroke="{a}" stroke-width="2" points="{pts}"/>'
        + f'<text x="{xs[0]:.1f}" y="{height - 6}" {font}>'
        f"{escape(str(series[0].get('date', '')))}</text>"
        + f'<text x="{xs[-1]:.1f}" y="{height - 6}" text-anchor="end" {font}>'
        f"{escape(str(series[-1].get('date', '')))}</text></svg>"
    )


# --- pages --------------------------------------------------------------------


def _masthead(title, summary):
    p = summary.get("period", {})
    when = f"{p.get('days', '?')} days to {str(p.get('end', ''))[:10]} (UTC)"
    body = (
        f"<div>{span(title, size=22, style='font-weight:bold')}</div>"
        f"<div>{span(when, THEME['muted'], size=12)}</div>"
        f"<div style='padding-top:8px'>{span(stats.momentum(summary), size=14)}</div>"
    )
    return f"<tr>{td(body, style='padding:0 0 8px')}</tr>"


def _counts(d):
    return sorted(d.items(), key=lambda kv: (-kv[1], str(kv[0])))


def _burndown(series, svg):
    inner = svg_burndown(series) + "<br>" if svg else ""
    inner += column_chart(series, "open", THEME["accent"])
    if series:
        first, last = series[0].get("open", 0), series[-1].get("open", 0)
        good = last < first
        word = "down" if good else "up" if last > first else "flat"
        line = span(
            f"Open {last}, {word} from {first}.", THEME["good"] if good else None
        )
        inner += f"<div style='padding-top:8px'>{line}</div>"
    return section("Team burndown", inner)


def _verification(summary, series):
    v = summary.get("verify", {})
    queue = v.get("queue", [])
    cov = v.get("coverage")
    qprev, qnow = summary.get("trend", {}).get("verify_queue", (None, len(queue)))
    delta = None if qprev is None else f"was {qprev}"
    inner = column_chart(series, "verify", THEME["warn"]) + "<br>"
    inner += tiles(
        stat_tile("in Verify", qnow, delta, good=qprev is not None and qnow < qprev),
        stat_tile("verified", v.get("verified", 0)),
        stat_tile("failed", v.get("failed", 0)),
        stat_tile("coverage", "–" if cov is None else f"{cov:.0%}"),
    )
    inner += sub("Oldest in the queue") + _rows(queue[:3], "verify", _days)
    return section("Verification", inner)


def _work(summary):
    o, f = summary.get("open", {}), summary.get("flow", {})
    columns = summary.get("columns", [])
    inner = sub("Open by column") + bars(
        _counts(o.get("by_column", {})), lambda c: column_color(c, columns)
    )
    inner += sub("Open by epic") + bars(_counts(o.get("by_epic", {})), THEME["accent"])
    inner += sub("WIP per person") + bars(_counts(f.get("wip", {})), THEME["accent"])
    return section("Where the work is", inner)


def _stuck(summary, person=None):
    f = summary.get("flow", {})
    src = person if person is not None else f
    overdue = src.get("overdue") if person else f.get("overdue_items", [])
    stuck = [
        {"iid": iid, "title": f"in {col}", "days": days}
        for iid, col, days in f.get("stuck", [])
    ]
    inner = sub("Overdue") + _rows(
        overdue or [], "due", lambda i: str(i.get("due", ""))
    )
    inner += sub("Questions waiting") + _rows(
        src.get("questions", []), "ask", lambda i: f"{i.get('author', '')} · {_days(i)}"
    )
    if person is None:
        inner += sub("Stuck") + _rows(stuck, "stuck", _days)
    return section("Stuck / questions", inner)


def _footer(summary):
    p = summary.get("period", {})
    text = (
        "Done = closed or moved to Done inside the window; the verify queue is "
        "what carries the Verify label now; ages are days. Window "
        f"{str(p.get('start', ''))[:10]} to {str(p.get('end', ''))[:10]}, UTC."
    )
    return (
        f"<tr>{td(span(text, THEME['muted'], size=11), style='padding:24px 0 0')}</tr>"
    )


def render_person_html(person, summary, username, series, svg=False, headers=None):
    """Moves and impact first, then the team picture."""
    blocks = [
        _masthead(username, summary),
        section("Your 3 moves", moves_block(stats.three_moves(person))),
        section(
            "Your impact",
            tiles(
                stat_tile("done", len(person.get("done", []))),
                stat_tile("verified", person.get("verified", 0)),
                stat_tile("failed", person.get("failed", 0)),
            ),
        ),
        _burndown(series, svg),
        _verification(summary, series),
        _work(summary),
        _stuck(summary, person),
        _footer(summary),
    ]
    return page(username, blocks, headers)


def render_team_html(summary, series, svg=False, headers=None):
    blocks = [
        _masthead("Team", summary),
        _burndown(series, svg),
        _verification(summary, series),
        _work(summary),
        _stuck(summary),
        _footer(summary),
    ]
    return page("Team", blocks, headers)


def render_index_html(entries):
    """One row per recipient with links to every file written for them."""
    rows = []
    for e in entries:
        links = " · ".join(
            f'<a href="{escape(path, quote=True)}" style="color:{THEME["accent"]};'
            f'font-family:{MONO};font-size:12px">{escape(ext)}</a>'
            for ext, path in sorted(e.get("files", {}).items())
        )
        cells = [
            td(span(e.get("name", ""), style="font-weight:bold"), width=120),
            td(span(e.get("to", ""), THEME["muted"], MONO, 12), width=180),
            td(span(e.get("subject", ""), size=12)),
            td(links, align="right"),
        ]
        rows.append(f"<tr>{''.join(cells)}</tr>")
    inner = table("".join(rows) if rows else _none("nothing written"))
    return page("Digest", [section("Digest", inner)])
