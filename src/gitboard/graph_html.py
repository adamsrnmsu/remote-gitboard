"""The blocker graph as one self-contained HTML page.

Inline SVG from `graph.build`'s layers, a native `<title>` per node for
hover, and a few lines of JS for click-to-highlight. No CDN and no vendored
library, so the file works air-gapped and as a digest attachment.
"""

import json
from collections import defaultdict
from html import escape

from gitboard.graph import _head, _p
from gitboard.mail import COLUMN_COLORS

DEFAULT = "#7f8ea3"
FLAG = "#d03b3b"
CARD_W, CARD_H, MS_W, MS_H = 200, 52, 220, 64

CSS = """
:root { --bg: #ffffff; --fg: #1d232b; --muted: #5d6875; --edge: #8a94a3; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #14171c; --fg: #e3e7ec; --muted: #9aa4b1; --edge: #6b7582; }
}
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 14px system-ui, sans-serif; }
h1 { font-size: 16px; margin: 0 0 12px; }
svg { display: block; }
.node { cursor: pointer; }
.node rect { stroke-width: 2; }
.node text { fill: var(--fg); font-size: 13px; }
.node text.sub { fill: var(--muted); font-size: 11px; }
.node.milestone text { font-weight: bold; }
.node.crit rect { stroke-width: 3; }
.node.flag rect { stroke: #d03b3b; }
.closed { opacity: .45; }
.edge { fill: none; stroke: var(--edge); stroke-width: 1.5; }
.edge.flag { stroke: #d03b3b; }
#arrow path { fill: var(--edge); }
.dim { opacity: .12; }
"""

JS = """
const svg = document.querySelector("svg");
function reach(start, adj) {
  const seen = new Set(), todo = [start];
  while (todo.length) {
    for (const n of adj[todo.pop()] || []) {
      if (!seen.has(n)) { seen.add(n); todo.push(n); }
    }
  }
  return seen;
}
function clear() {
  svg.querySelectorAll(".dim, .hl").forEach(el => el.classList.remove("dim", "hl"));
}
svg.addEventListener("click", e => {
  const g = e.target.closest("g[data-key]");
  if (!g) { clear(); return; }
  if (e.ctrlKey || e.metaKey) return;  // let the link open
  e.preventDefault();
  const key = g.dataset.key;
  const on = new Set([key, ...reach(key, UP), ...reach(key, DOWN)]);
  clear();
  svg.querySelectorAll("g[data-key]").forEach(el =>
    el.classList.add(on.has(el.dataset.key) ? "hl" : "dim"));
  svg.querySelectorAll("path[data-from]").forEach(el =>
    el.classList.add(on.has(el.dataset.from) && on.has(el.dataset.to) ? "hl" : "dim"));
});
"""


def _e(text):
    return escape(str(text), quote=True)


def _json(obj):
    """JSON safe inside <script>: no `<` survives, so neither `</script>`
    nor `<!--` in a new card's title can end or bend the script."""
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _cut(text, n=28):
    return text if len(text) <= n else text[: n - 1] + "…"


def _box(n, layer, pos):
    w, h = (MS_W, MS_H) if n["kind"] == "milestone" else (CARD_W, CARD_H)
    return 24 + layer * 240, 24 + pos * 72, w, h


def _node(n, box, classes):
    x, y, w, h = box
    colour = next(
        (COLUMN_COLORS[lb] for lb in n["labels"] if lb in COLUMN_COLORS), DEFAULT
    )
    head = _head(n)
    sub = [f"@{n['assignee']}"] if n["assignee"] else []
    if n["priority"] is not None:
        sub.append(_p(n["priority"]))
    if n["due_date"]:
        sub.append(f"due {n['due_date']}")
    tip = [head]
    if n["assignee"]:
        tip.append(f"@{n['assignee']}")
    if cols := [lb for lb in n["labels"] if "::" not in lb]:
        tip.append(", ".join(cols))
    if n["priority"] is not None:
        tip.append(_p(n["priority"]))
    if n["due_date"]:
        tip.append(f"due {n['due_date']}")
    rx = 32 if n["kind"] == "milestone" else 8
    ty = y + (h / 2 - 4 if sub else h / 2 + 5)
    body = (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
        f'fill="{colour}" fill-opacity=".18" stroke="{colour}"/>'
        f'<text x="{x + 12}" y="{ty}">{_e(_cut(head))}</text>'
    )
    if sub:
        body += (
            f'<text class="sub" x="{x + 12}" y="{ty + 17}">{_e(" · ".join(sub))}</text>'
        )
    url = n["web_url"] or ""
    if url.startswith(("https://", "http://")):
        body = f'<a href="{_e(url)}">{body}</a>'
    return (
        f'<g class="{" ".join(classes)}" data-key="{_e(n["key"])}">'
        f"<title>{_e(chr(10).join(tip))}</title>{body}</g>"
    )


def _edge(a, b, boxes, flagged):
    ax, ay, aw, ah = boxes[a]
    bx, by, _, bh = boxes[b]
    sx, sy, tx, ty = ax + aw, ay + ah / 2, bx, by + bh / 2
    mx = (sx + tx) / 2
    cls, marker = ("edge flag", "arrow-flag") if b in flagged else ("edge", "arrow")
    return (
        f'<path class="{cls}" data-from="{_e(a)}" data-to="{_e(b)}" '
        f'd="M{sx},{sy} C{mx},{sy} {mx},{ty} {tx},{ty}" '
        f'marker-end="url(#{marker})"/>'
    )


def render_html(g, title, flagged=frozenset()):
    """The whole page: blockers on the left, milestones as large nodes on
    the right; closed nodes faded, flagged ones and their edges red."""
    nodes = g["nodes"]
    boxes = {k: _box(nodes[k], *g["layers"][k]) for k in nodes}
    crit = {k for path in g["critical"].values() for k in path}
    width = max((x + w for x, _, w, _ in boxes.values()), default=0) + 24
    height = max((y + h for _, y, _, h in boxes.values()), default=0) + 24
    up, down = defaultdict(list), defaultdict(list)
    for a, b in g["edges"]:
        up[b].append(a)
        down[a].append(b)
    parts = []
    for k, n in nodes.items():
        on = {"closed": not n["open"], "flag": k in flagged, "crit": k in crit}
        classes = ["node", *(c for c, yes in on.items() if yes), n["kind"]]
        parts.append(_node(n, boxes[k], classes))
    edges = [_edge(a, b, boxes, flagged) for a, b in g["edges"]]
    markers = "".join(
        f'<marker id="{mid}" viewBox="0 0 10 10" refX="10" refY="5" '
        f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0,0 L10,5 L0,10 z"{fill}/></marker>'
        for mid, fill in (("arrow", ""), ("arrow-flag", f' fill="{FLAG}"'))
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title><style>{CSS}</style></head><body>"
        f"<h1>{_e(title)}</h1>"
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f"<defs>{markers}</defs>{''.join(edges)}{''.join(parts)}</svg>"
        f"<script>const UP = {_json(up)}, DOWN = {_json(down)};\n{JS}</script>"
        "</body></html>\n"
    )
