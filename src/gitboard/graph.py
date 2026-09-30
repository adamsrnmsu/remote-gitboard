"""The blocker graph: which card waits on which, converging on milestones.

Pure. Input is the card shape `board.fetch_history` returns (or
`cards_from_spec` builds from a YAML); output is a model, the flags where
the board contradicts itself, a rich tree and Mermaid. Keys are refs ("12",
"new:<title>", "grp/x#4") plus "m:<milestone>" for milestones.
"""

import datetime
import graphlib
import re
from collections import defaultdict

from rich.console import Group
from rich.text import Text
from rich.tree import Tree

from gitboard import links

DONE, FAILED, VERIFY, BLOCKED = "Done", "Failed", "Verify", "Blocked"
FLAGS = (
    "blocked_stale",
    "blocked_unmarked",
    "priority_inversion",
    "date_inversion",
    "unowned_blocker",
    "no_milestone",
)


def is_open(card):
    """stats.done_at's rule: a card in Done is finished even while open."""
    return card["state"] == "opened" and DONE not in card["labels"]


def _key(card):
    return str(card["iid"]) if card["iid"] is not None else f"new:{card['title']}"


def _name(d):
    """`#12` for a numbered card, else its title (a ref for an external)."""
    return f"#{d['iid']}" if d.get("iid") is not None else d["title"]


def _rank(p):
    return p if p is not None else 5


def _p(p):
    return f"P{p}" if p is not None else "P–"


def cards_from_spec(spec, url):
    """The card shape from a pulled YAML, so `graph --from` needs no network.
    A same-project blocker missing from the spec is closed: every open issue
    is on the board, so a pull would have written it."""
    project = spec["project"]
    titles = {i["title"].strip(): i.get("iid") for i in spec["issues"]}
    done = {
        str(i["iid"]): DONE in i.get("labels", [])
        for i in spec["issues"]
        if i.get("iid") is not None
    }
    due = {m["title"]: _iso(m.get("due_date")) for m in spec.get("milestones") or []}

    def state(ref):
        if links._is_iid(ref):
            return "closed" if done.get(ref, True) else "opened"
        return "opened" if ref.startswith("new:") else None

    cards = []
    for i in spec["issues"]:
        iid, labels = i.get("iid"), list(i.get("labels", []))
        refs = links.norm_refs(i.get("blocked_by"), project, titles)
        cards.append(
            {
                "iid": iid,
                "title": i["title"].strip(),
                "state": "opened",
                "labels": labels,
                "assignee": i.get("assignee"),
                "due_date": _iso(i.get("due_date")),
                "milestone": i.get("milestone"),
                "milestone_due": due.get(i.get("milestone")),
                "priority": links.priority(labels),
                "blocked_by": [
                    {"ref": r, "state": state(r), "since": None} for r in refs
                ],
                "web_url": f"{url}/{project}/-/issues/{iid}" if iid else None,
            }
        )
    return cards


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def _node(key, kind, *, title, open, iid=None, due_date=None, **rest):
    base = dict.fromkeys(("assignee", "priority", "milestone", "web_url"))
    return {
        **base,
        "key": key,
        "kind": kind,
        "iid": iid,
        "title": title,
        "open": open,
        "labels": [],
        "due_date": due_date,
        **rest,
    }


def build(cards):
    """Nodes and edges (blocker -> card, card -> milestone), then the
    derived counts, chains and layout."""
    nodes, edges = {}, []
    for c in cards:
        nodes[_key(c)] = _node(
            _key(c),
            "card",
            iid=c["iid"],
            title=c["title"],
            open=is_open(c),
            assignee=c.get("assignee"),
            labels=list(c["labels"]),
            due_date=c.get("due_date"),
            priority=c.get("priority"),
            milestone=c.get("milestone"),
            web_url=c.get("web_url"),
        )
    for c in cards:
        for b in c.get("blocked_by", []):
            ref = b["ref"]
            if ref not in nodes:
                nodes[ref] = (
                    _node(ref, "card", iid=int(ref), title=f"#{ref}", open=False)
                    if links._is_iid(ref)
                    else _node(ref, "external", title=ref, open=b["state"] != "closed")
                )
            edges.append((ref, _key(c)))
        if m := c.get("milestone"):
            mk = f"m:{m}"
            if mk not in nodes:
                nodes[mk] = _node(
                    mk,
                    "milestone",
                    title=m,
                    open=False,
                    due_date=c.get("milestone_due"),
                )
            nodes[mk]["open"] |= is_open(c)
            edges.append((_key(c), mk))
    return _derive(nodes, edges)


def subgraph(g, milestone_title):
    """The milestone and everything upstream of it, re-derived."""
    preds = _preds(g["edges"])
    keep, todo = set(), [f"m:{milestone_title}"]
    while todo:
        k = todo.pop()
        if k in g["nodes"] and k not in keep:
            keep.add(k)
            todo.extend(preds[k])
    nodes = {k: v for k, v in g["nodes"].items() if k in keep}
    return _derive(nodes, [e for e in g["edges"] if e[0] in keep and e[1] in keep])


def _preds(edges):
    preds = defaultdict(list)
    for a, b in edges:
        preds[b].append(a)
    return preds


def _succs(edges):
    succs = defaultdict(list)
    for a, b in edges:
        succs[a].append(b)
    return succs


def _depths(nodes, edges):
    """Longest-path depth per node; returns (depth, edges without cycles)."""
    edges = list(dict.fromkeys(edges))
    while True:
        preds = _preds(edges)
        try:
            order = list(
                graphlib.TopologicalSorter({k: preds[k] for k in nodes}).static_order()
            )
            break
        except graphlib.CycleError as e:
            # ponytail: links.check stops spec cycles; one made in the GitLab
            # UI loses a back-edge here rather than breaking the view.
            cycle = e.args[1]
            edges.remove((cycle[-2], cycle[-1]))
    depth = {}
    for k in order:
        depth[k] = 1 + max((depth[p] for p in preds[k]), default=-1)
    return depth, edges


def _derive(nodes, edges):
    depth, edges = _depths(nodes, edges)
    preds, succs = _preds(edges), _succs(edges)
    downstream = {}
    for k in nodes:
        seen, todo = set(), list(succs[k])
        while todo:
            n = todo.pop()
            if n not in seen:
                seen.add(n)
                todo.extend(succs[n])
        downstream[k] = sum(nodes[n]["kind"] != "milestone" for n in seen)
    critical = {}
    for k, n in nodes.items():
        if n["kind"] == "milestone":
            path = [k]
            while preds[path[0]]:
                best = max(depth[p] for p in preds[path[0]])
                path.insert(
                    0,
                    min(
                        (p for p in preds[path[0]] if depth[p] == best),
                        key=links.ref_key,
                    ),
                )
            critical[k] = path
    return {
        "nodes": nodes,
        "edges": edges,
        "downstream": downstream,
        "critical": critical,
        "layers": _layers(nodes, depth, preds, succs),
    }


def _layers(nodes, depth, preds, succs):
    """Longest-path layers, milestones last, then two barycenter sweeps:
    a deliberately small Sugiyama that keeps crossings down."""
    last = 1 + max(
        (d for k, d in depth.items() if nodes[k]["kind"] != "milestone"), default=-1
    )
    layer = {k: last if nodes[k]["kind"] == "milestone" else depth[k] for k in nodes}
    rows = defaultdict(list)
    for k in sorted(nodes, key=links.ref_key):
        rows[layer[k]].append(k)
    pos = {k: i for row in rows.values() for i, k in enumerate(row)}

    def sweep(i, nbrs):
        def bary(k):
            ns = nbrs[k]
            return sum(pos[n] for n in ns) / len(ns) if ns else pos[k]

        rows[i].sort(key=lambda k: (bary(k), links.ref_key(k)))
        pos.update({k: j for j, k in enumerate(rows[i])})

    top = max(rows, default=0)
    for i in range(1, top + 1):
        sweep(i, preds)
    for i in range(top - 1, -1, -1):
        sweep(i, succs)
    return {k: (layer[k], pos[k]) for k in nodes}


def _pair(b, c, bdate, cdate):
    """Contradictions between an open blocker `b` and the open card `c` it
    blocks, shared by `flags` and the tree's ⚑."""
    out = []
    if _rank(b["priority"]) > _rank(c["priority"]):
        out.append(
            (
                "priority_inversion",
                f"{_name(c)} ({_p(c['priority'])}) waits on "
                f"{_name(b)} ({_p(b['priority'])})",
            )
        )
    if bdate and cdate and bdate > cdate:
        out.append(
            (
                "date_inversion",
                f"{_name(c)} due {cdate} waits on {_name(b)} due {bdate}",
            )
        )
    if b["assignee"] is None and c["milestone"]:
        out.append(
            (
                "unowned_blocker",
                f"{_name(b)} (unassigned) blocks {_name(c)} in {c['milestone']}",
            )
        )
    return out


def flags(cards, columns):
    """Where the board contradicts its own links. Flags, never moves."""
    by_key = {_key(c): c for c in cards}
    blocked = {c for c in columns if c.lower() == BLOCKED.lower()}
    parked = blocked | {VERIFY, DONE, FAILED}
    out = {f: [] for f in FLAGS}

    def item(card, blocker, detail):
        return {
            "iid": card["iid"],
            "title": card["title"],
            "assignee": card.get("assignee"),
            "url": card.get("web_url"),
            "blocker": blocker,
            "detail": detail,
        }

    def date(c):
        return c.get("due_date") or c.get("milestone_due")

    planned = any(c.get("milestone") for c in cards)
    for c in cards:
        if not is_open(c):
            continue
        if (
            planned
            and not c.get("milestone")
            and not {VERIFY, FAILED} & set(c["labels"])
        ):
            out["no_milestone"].append(item(c, None, f"{_name(c)} has no milestone"))
        refs = c.get("blocked_by", [])
        live = [
            b["ref"]
            for b in refs
            if (
                is_open(by_key[b["ref"]])
                if b["ref"] in by_key
                else b["state"] != "closed"
            )
        ]
        if blocked and set(c["labels"]) & blocked and not live:
            out["blocked_stale"].append(
                item(c, None, f"{_name(c)} is in Blocked, no open blocker")
            )
        if blocked and live and not set(c["labels"]) & parked:
            shown = ", ".join(_shown(r, by_key) for r in live)
            out["blocked_unmarked"].append(
                item(c, live[0], f"{_name(c)} waits on {shown}, not in Blocked")
            )
        for ref in live:
            if ref not in by_key:
                continue
            b = by_key[ref]
            for kind, detail in _pair(b, c, date(b), date(c)):
                if kind == "unowned_blocker":
                    out[kind].append(item(b, _key(c), detail))
                else:
                    out[kind].append(item(c, ref, detail))
    for items in out.values():
        items.sort(key=lambda x: (x["iid"] is None, x["iid"] or 0, x["blocker"] or ""))
    return out


def _shown(ref, by_key):
    return _name(by_key[ref]) if ref in by_key else links._shown(ref)


def _head(n):
    """`#12 Title`, or just the name when the title is the name."""
    name = _name(n)
    return name if name == n["title"] else f"{name} {n['title']}"


def render_tree(g, today, flagged=frozenset()):
    """One tree per milestone, blockers nested under what they block.
    `flagged` is the keys `flags` names (the caller has the columns), ⚑."""
    nodes, preds = g["nodes"], _preds(g["edges"])
    succs = _succs(g["edges"])
    star = {k for path in g["critical"].values() for k in path}
    printed = set()

    def line(k):
        n = nodes[k]
        parts = [_head(n)]
        if n["assignee"]:
            parts.append(f"@{n['assignee']}")
        if n["priority"] is not None:
            parts.append(_p(n["priority"]))
        if n["due_date"]:
            parts.append(f"due {n['due_date']}")
        if waiting := g["downstream"].get(k):
            parts.append(f"⇠ {waiting} waiting")
        text = "  ".join(parts) + (" ★" if k in star else "")
        if n["open"] and k in flagged:
            text += "  ⚑"
        return Text(text, "" if n["open"] else "muted")

    def add(parent, k):
        if k in printed:
            parent.add(Text(f"(see {_name(nodes[k])} above)", "muted"))
            return
        printed.add(k)
        branch = parent.add(line(k))
        for p in sorted(preds[k], key=links.ref_key):
            add(branch, p)

    trees = []
    ms = [n for n in nodes.values() if n["kind"] == "milestone"]
    for m in sorted(ms, key=lambda n: (n["due_date"] or "9999", n["title"])):
        mine = [k for k in preds[m["key"]] if nodes[k]["kind"] != "milestone"]
        tree = Tree(Text(_root(m, mine, nodes, today), "bold"))
        blockers = {p for k in mine for p in preds[k]}
        for k in sorted((k for k in mine if k not in blockers), key=links.ref_key):
            add(tree, k)
        trees.append(tree)
    rest = {
        k
        for k, n in nodes.items()
        if n["kind"] != "milestone"
        and not n["milestone"]
        and k not in printed
        and (preds[k] or succs[k])
    }
    tops = [k for k in rest if not any(s in rest for s in succs[k])]
    if tops:
        tree = Tree(Text("No milestone", "bold"))
        for k in sorted(tops, key=links.ref_key):
            add(tree, k)
        trees.append(tree)
    return Group(*trees)


def _root(m, mine, nodes, today):
    parts = [f"◆ {m['title']}"]
    if m["due_date"]:
        days = (
            datetime.date.fromisoformat(m["due_date"])
            - datetime.date.fromisoformat(today)
        ).days
        left = f"{days} days left" if days >= 0 else f"{-days} days late"
        parts.append(f"due {m['due_date']} · {left}")
    if mine:
        done = sum(not nodes[k]["open"] for k in mine)
        parts.append(f"{done}/{len(mine)} done")
    return parts[0] + ("  " + " · ".join(parts[1:]) if parts[1:] else "")


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower())


def _mid(n):
    """Mermaid ids are bare words, so every key maps to [a-z0-9_]."""
    if n["kind"] == "milestone":
        return _slug(n["key"])
    if n["kind"] == "external":
        return "x_" + _slug(n["key"])
    return f"i{n['iid']}" if n["iid"] is not None else "n_" + _slug(n["title"])


def _esc(text):
    """Mermaid entity escapes; `#` first, since the others add one."""
    for a, b in (("#", "#35;"), ('"', "#quot;"), ("<", "#lt;"), (">", "#gt;")):
        text = text.replace(a, b)
    return text


def render_mermaid(g):
    """`flowchart LR`, pasteable into a GitLab description or wiki page."""
    nodes = g["nodes"]
    keys = sorted(nodes, key=links.ref_key)
    ids, used = {}, set()
    for k in keys:
        # A slug drops non-ASCII, so "日本" and "中文" both slug to "n__";
        # a suffix keeps them two nodes instead of one merged one.
        base = mid = _mid(nodes[k])
        while mid in used:
            mid = f"{base}_{len(used)}"
        ids[k] = mid
        used.add(mid)
    lines = ["flowchart LR"]
    for k in keys:
        n = nodes[k]
        if n["kind"] == "milestone":
            label = n["title"] + (f" · due {n['due_date']}" if n["due_date"] else "")
            lines.append(f'  {ids[k]}(["{_esc(label)}"])')
        else:
            lines.append(f'  {ids[k]}["{_esc(_head(n))}"]')
    lines += [f"  {ids[a]} --> {ids[b]}" for a, b in g["edges"]]
    if done := [ids[k] for k in keys if not nodes[k]["open"]]:
        lines.append(f"  class {','.join(done)} done;")
        lines.append("  classDef done fill:#eeeeee,color:#888888;")
    return "\n".join(lines) + "\n"
