"""graph_html.py: one self-contained page; no external fetches; escaped."""

import re
from html.parser import HTMLParser

from gitboard import graph, graph_html


def card(
    iid,
    title=None,
    *,
    labels=(),
    blocked_by=(),
    milestone=None,
    milestone_due=None,
    assignee="alice",
    due=None,
    priority=None,
    state="opened",
    web_url=None,
):
    """Copied from tests/test_graph.py: tests/ is not a package."""
    return {
        "iid": iid,
        "title": title or f"card {iid}",
        "state": state,
        "labels": list(labels),
        "assignee": assignee,
        "due_date": due,
        "milestone": milestone,
        "milestone_due": milestone_due,
        "priority": priority,
        "blocked_by": [{"ref": r, "state": s, "since": None} for r, s in blocked_by],
        "web_url": web_url,
    }


# 1 -> 2 -> 4 -> Beta ; 3 -> 4 ; 5 -> Beta ; external x/y#7 -> 5
CARDS = [
    card(1),
    card(2, blocked_by=[("1", "opened")]),
    card(3),
    card(
        4,
        blocked_by=[("2", "opened"), ("3", "opened")],
        milestone="Beta",
        milestone_due="2026-11-01",
    ),
    card(5, blocked_by=[("x/y#7", None)], milestone="Beta", milestone_due="2026-11-01"),
]


class Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.srcs, self.hrefs, self.anchors = [], [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        a = dict(attrs)
        if "src" in a:
            self.srcs.append(a["src"])
        if tag == "link" and "href" in a:
            self.hrefs.append(a["href"])
        if tag == "a":
            self.anchors.append(a.get("href"))


def test_page_is_self_contained():
    page = graph_html.render_html(graph.build(CARDS), "grp/proj — Dev")
    s = Scan()
    s.feed(page)
    assert s.tags.count("svg") == 1 and "script" in s.tags
    assert s.srcs == [] and s.hrefs == []
    assert "prefers-color-scheme: dark" in page
    assert 'data-key="m:Beta"' in page
    assert "http" not in page


def test_titles_are_escaped():
    cards = [
        card(1, '<script>alert(1)</script> & "q"'),
        card(2, blocked_by=[("1", "opened")], milestone="Beta"),
    ]
    page = graph_html.render_html(graph.build(cards), "t")
    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page


def test_new_card_key_cannot_close_the_script():
    """A spec card with no iid is keyed by its title, which lands in the
    adjacency JSON inside <script>."""
    cards = [
        card(None, "</script><b>x"),
        card(2, blocked_by=[("new:</script><b>x", "opened")]),
    ]
    page = graph_html.render_html(graph.build(cards), "t")
    script = page.split("<script>", 1)[1]
    assert script.count("</script>") == 1
    assert "<b>" not in page


def test_flagged_and_closed_classes():
    cards = [
        card(1, state="closed"),
        card(2, blocked_by=[("1", "closed")], milestone="Beta"),
    ]
    page = graph_html.render_html(graph.build(cards), "t", flagged={"2"})
    assert 'class="node closed' in page and "flag" in page
    assert re.search(r'<path class="edge flag"[^>]*data-to="2"', page)


def test_critical_chain_is_marked():
    page = graph_html.render_html(graph.build(CARDS), "t")
    for key in ("1", "2", "4", "m:Beta"):
        assert re.search(
            rf'class="node[^"]*crit[^"]*" data-key="{re.escape(key)}"', page
        )
    assert not re.search(r'class="node[^"]*crit[^"]*" data-key="3"', page)


def test_positions_and_size_follow_layers():
    g = graph.build(CARDS)
    page = graph_html.render_html(g, "t")
    layer, pos = g["layers"]["4"]
    assert f'x="{24 + layer * 240}" y="{24 + pos * 72}" width="200" height="52"' in page
    ml, mp = g["layers"]["m:Beta"]
    assert f'x="{24 + ml * 240}" y="{24 + mp * 72}" width="220" height="64"' in page
    rows = max(p for _, p in g["layers"].values())
    width = 24 + ml * 240 + 220 + 24
    assert f'width="{width}"' in page
    assert f'height="{max(24 + rows * 72 + 52, 24 + mp * 72 + 64) + 24}"' in page


def test_labels_truncate_and_carry_details():
    long = "A very long card title that keeps on going"
    cards = [
        card(12, long, labels=["Doing", "priority::1"], priority=1, due="2026-10-20")
    ]
    page = graph_html.render_html(graph.build(cards), "t")
    head = f"#12 {long}"
    assert head[:27] + "…" in page
    assert "@alice · P1 · due 2026-10-20" in page
    assert f"<title>{head}\n" in page  # the full title is in the tooltip
    assert 'fill="#3987e5" fill-opacity=".18" stroke="#3987e5"' in page


def test_only_http_links_are_emitted():
    cards = [
        card(1, web_url="https://gitlab.example/g/p/-/issues/1"),
        card(2, web_url="javascript:alert(1)"),
    ]
    s = Scan()
    s.feed(graph_html.render_html(graph.build(cards), "t"))
    assert s.anchors == ["https://gitlab.example/g/p/-/issues/1"]


def test_milestone_only_graph_renders():
    g = graph.build([], [{"title": "Later"}])
    page = graph_html.render_html(g, "t")
    assert 'data-key="m:Later"' in page and "closed" not in page.split("<svg")[1]
