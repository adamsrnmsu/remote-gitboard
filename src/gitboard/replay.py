"""The board as a timelapse: snapshots.jsonl -> frames -> one HTML page.

`frames` is pure (batches in, frames out). The page is one self-contained
file in graph_html's mould: inline CSS and JS, no CDN, data embedded with
`_json` so a card title cannot end the script. Local files only; no person
tallies — the assignee is a card attribute and nothing more.
"""

from html import escape

from gitboard.graph_html import _json
from gitboard.mail import column_color


def frames(batches):
    """([{ts, columns, moved, new, closed}], {iid: {title, assignee, due_date}}).

    Columns are every lane ever seen (Backlog first, then first-seen), in every
    frame, so lanes never jump. A two-column card sits in both lists. Counts
    compare with the previous kept frame; a frame equal to it is dropped.
    """
    order, cards, out, prev = [], {}, [], {}
    for batch in batches:
        if not batch:
            continue
        for rec in batch.values():
            for c in rec["columns"]:
                if c not in order:
                    order.append(c)
            cards[rec["iid"]] = {k: rec[k] for k in ("title", "assignee", "due_date")}
        now = {i: r["columns"] for i, r in batch.items()}
        if out and now == prev:
            continue
        out.append(
            {
                "ts": next(iter(batch.values()))["ts"],
                "columns": {
                    c: sorted(i for i, cs in now.items() if c in cs) for c in order
                },
                "moved": sum(1 for i in now if i in prev and now[i] != prev[i]),
                "new": len(now.keys() - prev.keys()) if out else 0,
                "closed": len(prev.keys() - now.keys()),
            }
        )
        prev = now
    order.sort(key=lambda c: c != "Backlog")  # stable: Backlog first
    for f in out:
        f["columns"] = {c: f["columns"].get(c, []) for c in order}
    return out, cards


CSS = """
:root { --bg: #ffffff; --fg: #1d232b; --muted: #5d6875; --card: #f3f5f8; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #14171c; --fg: #e3e7ec; --muted: #9aa4b1; --card: #1f242b; }
}
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg);
  font: 14px system-ui, sans-serif; }
h1 { font-size: 16px; margin: 0 0 12px; }
#bar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
  margin-bottom: 12px; }
#scrub { flex: 1; min-width: 200px; }
#when { font-variant-numeric: tabular-nums; }
#counts { color: var(--muted); }
#stage { position: relative; }
.lane { position: absolute; top: 0; bottom: 0; border-radius: 8px;
  background: var(--card); opacity: .5; }
.head { position: absolute; top: 8px; font-weight: bold; font-size: 13px; }
.card { position: absolute; left: 0; top: 0; box-sizing: border-box;
  padding: 6px 8px; border: 2px solid; border-radius: 8px;
  background: var(--bg); color: var(--fg); text-decoration: none;
  overflow: hidden; font-size: 12px;
  transition: transform .6s ease, opacity .6s ease; }
.card .sub { color: var(--muted); font-size: 11px; display: block; }
.card b { font-weight: 600; }
@media (prefers-reduced-motion: reduce) { .card { transition: none; } }
"""

JS = """
const LW = 220, CW = 200, CH = 62, TOP = 36;
const stage = document.getElementById("stage");
const names = Object.keys(FRAMES[0].columns);
let rows = 1;
FRAMES.forEach(f => names.forEach(n => {
  rows = Math.max(rows, f.columns[n].length);
}));
stage.style.height = (TOP + rows * (CH + 6) + 8) + "px";
stage.style.width = (names.length * LW) + "px";
names.forEach((n, i) => {
  const lane = document.createElement("div");
  lane.className = "lane"; lane.style.left = (i * LW) + "px";
  lane.style.width = (LW - 8) + "px";
  const h = document.createElement("div");
  h.className = "head"; h.textContent = n; h.style.left = (i * LW + 8) + "px";
  h.style.color = COLORS[n];
  stage.append(lane, h);
});
const els = {};
function el(iid, n) {
  const k = iid + "|" + n;
  if (els[k]) return els[k];
  const c = CARDS[iid], url = URLS[iid];
  const a = document.createElement(url ? "a" : "div");
  if (url) a.setAttribute("href", url);
  a.className = "card"; a.style.borderColor = COLORS[n]; a.style.opacity = 0;
  a.style.pointerEvents = "none"; a.style.width = CW + "px";
  a.style.height = CH + "px";
  const b = document.createElement("b"); b.textContent = "#" + iid + " " + c.title;
  const s = document.createElement("span"); s.className = "sub";
  s.textContent = [c.assignee ? "@" + c.assignee : "",
    c.due_date ? "due " + c.due_date : ""]
    .filter(Boolean).join(" \\u00b7 ");
  a.append(b, s);
  a.title = b.textContent + (s.textContent ? "\\n" + s.textContent : "");
  a.style.transform = "translate(" + (names.indexOf(n) * LW + 8) + "px, " + TOP + "px)";
  stage.append(a);
  return els[k] = a;
}
const scrub = document.getElementById("scrub"), when = document.getElementById("when");
const counts = document.getElementById("counts");
const play = document.getElementById("play");
scrub.max = FRAMES.length - 1;
let at = 0, timer = null;
function show(i) {
  at = i; scrub.value = i;
  const f = FRAMES[i], live = new Set();
  names.forEach((n, x) => f.columns[n].forEach((iid, r) => {
    const a = el(iid, n); live.add(a);
    const y = TOP + r * (CH + 6);
    a.style.transform = "translate(" + (x * LW + 8) + "px, " + y + "px)";
    a.style.opacity = 1; a.style.pointerEvents = "auto";
  }));
  Object.values(els).forEach(a => {
    if (!live.has(a)) { a.style.opacity = 0; a.style.pointerEvents = "none"; }
  });
  when.textContent = f.ts;
  counts.textContent = "moved " + f.moved + ", new " + f.new + ", closed " + f.closed
    + "  (" + (i + 1) + "/" + FRAMES.length + ")";
}
function stop() { clearTimeout(timer); timer = null; play.textContent = "Play"; }
function tick() {
  if (at >= FRAMES.length - 1) { stop(); return; }
  show(at + 1);
  timer = setTimeout(tick, 1200 / Number(document.getElementById("speed").value));
}
function toggle() {
  if (timer) { stop(); return; }
  if (at >= FRAMES.length - 1) show(0);
  play.textContent = "Pause";
  timer = setTimeout(tick, 600);
}
play.addEventListener("click", toggle);
scrub.addEventListener("input", () => { stop(); show(Number(scrub.value)); });
document.addEventListener("keydown", e => {
  const t = e.target.tagName;
  if (e.code === "Space" && t !== "INPUT" && t !== "SELECT" && t !== "BUTTON") {
    e.preventDefault(); toggle();
  }
});
show(0);
"""


def render_html(frames_, cards, title, base_url=None, project=None):
    """The whole page. A card links only when `base_url` is http(s)."""
    web = base_url and base_url.startswith(("https://", "http://"))
    urls = (
        {i: f"{base_url.rstrip('/')}/{project}/-/issues/{i}" for i in cards}
        if web and project
        else {}
    )
    names = list(frames_[0]["columns"])
    colors = {n: column_color(n, names) for n in names}
    data = (
        f"const FRAMES = {_json(frames_)}, CARDS = {_json(cards)}, "
        f"URLS = {_json(urls)}, COLORS = {_json(colors)};\n"
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)}</title><style>{CSS}</style></head><body>"
        f"<h1>{escape(title)}</h1>"
        '<div id="bar"><button id="play" type="button">Play</button>'
        '<input id="scrub" type="range" min="0" max="0" value="0">'
        '<label>speed <select id="speed"><option value="0.5">0.5x</option>'
        '<option value="1" selected>1x</option><option value="2">2x</option>'
        '<option value="4">4x</option></select></label>'
        '<span id="when"></span><span id="counts"></span></div>'
        '<div id="stage"></div>'
        f"<script>{data}{JS}</script>"
        "</body></html>\n"
    )
