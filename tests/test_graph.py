"""graph.py: model, flags, spec cards, tree and Mermaid. Pure."""

import datetime

from rich.console import Console

from gitboard import graph


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
):
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
        "web_url": None,
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


def test_build_edges_and_external_nodes():
    g = graph.build(CARDS)
    assert ("1", "2") in g["edges"] and ("4", "m:Beta") in g["edges"]
    assert g["nodes"]["x/y#7"]["kind"] == "external"
    assert g["nodes"]["m:Beta"]["kind"] == "milestone"


def test_downstream_counts_cards_not_milestones():
    g = graph.build(CARDS)
    assert g["downstream"]["1"] == 2  # 2 and 4
    assert g["downstream"]["3"] == 1
    assert g["downstream"]["4"] == 0


def test_critical_chain_is_longest_with_lowest_key_tiebreak():
    g = graph.build(CARDS)
    assert g["critical"]["m:Beta"] == ["1", "2", "4", "m:Beta"]


def test_layers_put_milestones_last():
    g = graph.build(CARDS)
    layers = {k: v[0] for k, v in g["layers"].items()}
    assert layers["1"] == 0 and layers["2"] == 1 and layers["4"] == 2
    assert layers["m:Beta"] == max(layers.values())


def test_closed_same_project_blocker_absent_from_cards():
    g = graph.build([card(2, blocked_by=[("1", "closed")])])
    assert g["nodes"]["1"]["open"] is False and g["nodes"]["1"]["title"] == "#1"


def test_flags():
    cards = [
        card(1, priority=3, due="2026-11-05", assignee=None),
        card(
            2,
            labels=["Doing"],
            priority=1,
            due="2026-10-20",
            blocked_by=[("1", "opened")],
            milestone="Beta",
        ),
        card(3, labels=["Blocked"], blocked_by=[("9", "closed")]),
    ]
    f = graph.flags(cards, ["Doing", "Blocked", "Verify"])
    assert [x["iid"] for x in f["blocked_stale"]] == [3]
    assert [x["iid"] for x in f["blocked_unmarked"]] == [2]
    assert f["priority_inversion"][0]["detail"] == "#2 (P1) waits on #1 (P3)"
    assert (
        f["date_inversion"][0]["detail"]
        == "#2 due 2026-10-20 waits on #1 due 2026-11-05"
    )
    assert f["unowned_blocker"][0]["iid"] == 1
    assert f["unowned_blocker"][0]["detail"] == "#1 (unassigned) blocks #2 in Beta"


def test_no_milestone_flag():
    cards = [
        card(1, milestone="Beta"),
        card(2),
        card(3, labels=["Done"]),
        card(4, state="closed"),
        card(5, labels=["Verify"]),
    ]
    assert [x["iid"] for x in graph.flags(cards, ["Doing"])["no_milestone"]] == [2]
    assert graph.flags(cards, ["Doing"])["no_milestone"][0]["detail"] == (
        "#2 has no milestone"
    )
    assert graph.flags(cards[1:], ["Doing"])["no_milestone"] == []
    # a milestone only a finished card carries plans nothing
    old = [card(1, milestone="Beta", state="closed"), card(2)]
    assert graph.flags(old, ["Doing"])["no_milestone"] == []


def test_no_blocked_flags_without_a_blocked_column():
    f = graph.flags(
        [card(3, labels=["Doing"], blocked_by=[("9", "opened")])], ["Doing"]
    )
    assert f["blocked_stale"] == [] and f["blocked_unmarked"] == []


def test_closed_blocker_raises_no_inversion():
    cards = [
        card(1, priority=4, state="closed"),
        card(2, priority=1, blocked_by=[("1", "closed")]),
    ]
    assert graph.flags(cards, ["Doing"])["priority_inversion"] == []


SPEC = {
    "project": "grp/proj",
    "board": "b",
    "columns": [{"name": "Doing"}, {"name": "Done"}],
    "milestones": [{"title": "Beta", "due_date": datetime.date(2026, 11, 1)}],
    "issues": [
        {
            "title": "reader",
            "iid": 12,
            "labels": ["priority::1"],
            "milestone": "Beta",
            "blocked_by": [9, "Token rotation", "infra/platform#4", "Brand new"],
        },
        {"title": "Token rotation", "iid": 14, "labels": ["Done"]},
        {"title": "Brand new"},
    ],
}


def test_cards_from_spec():
    cards = {c["title"]: c for c in graph.cards_from_spec(SPEC, "https://gl")}
    r = cards["reader"]
    assert r["priority"] == 1 and r["milestone_due"] == "2026-11-01"
    assert [(b["ref"], b["state"]) for b in r["blocked_by"]] == [
        ("9", "closed"),
        ("14", "closed"),
        ("infra/platform#4", None),
        ("new:Brand new", "opened"),
    ]
    assert cards["Brand new"]["iid"] is None


def test_subgraph_keeps_upstream_only():
    g = graph.subgraph(graph.build(CARDS + [card(8, milestone="GA")]), "Beta")
    assert "8" not in g["nodes"] and "m:GA" not in g["nodes"] and "1" in g["nodes"]


def _text(renderable):
    c = Console(width=120, record=True, color_system=None)
    c.print(renderable)
    return c.export_text()


def test_tree_prints_shared_blocker_once():
    shared = [
        card(1),
        card(2, blocked_by=[("1", "opened")], milestone="Beta"),
        card(3, blocked_by=[("1", "opened")], milestone="Beta"),
    ]
    text = _text(graph.render_tree(graph.build(shared), "2026-09-27"))
    assert "◆ Beta" in text
    assert text.count("#1 card 1") == 1 and "(see #1 above)" in text


def test_mermaid_escapes_hostile_titles():
    cards = [
        card(1, 'say "hi" <script>#x'),
        card(2, blocked_by=[("1", "closed")], milestone="Beta"),
    ]
    cards[0]["state"] = "closed"
    text = graph.render_mermaid(graph.build(cards))
    assert text.startswith("flowchart LR")
    assert "<script>" not in text and '"hi"' not in text
    assert "i1 --> i2" in text and "i2 --> m_beta" in text
    assert "class i1 done;" in text


def test_mermaid_ids_stay_distinct_for_unicode_titles():
    cards = [
        card(None, "日本", milestone="Выпуск"),
        card(None, "中文", milestone="Релиз"),
    ]
    lines = graph.render_mermaid(graph.build(cards)).splitlines()
    ids = [ln.split("[")[0].split("(")[0].strip() for ln in lines[1:5]]
    assert len(set(ids)) == 4, ids


def test_offline_closed_blocker_is_faded_and_raises_no_flag():
    spec = {
        "project": "grp/proj",
        "issues": [
            {
                "title": "reader",
                "iid": 12,
                "labels": ["Doing", "priority::1"],
                "blocked_by": [9],
            },
        ],
    }
    cards = graph.cards_from_spec(spec, "https://gl")
    f = graph.flags(cards, ["Doing", "Blocked"])
    assert f["blocked_unmarked"] == [] and f["priority_inversion"] == []
    g = graph.build(cards)
    assert g["nodes"]["9"]["open"] is False
    assert "class i9 done;" in graph.render_mermaid(g)


def test_cards_from_spec_takes_an_empty_milestones_key():
    """`milestones:` with nothing under it is None in YAML; load() takes it."""
    spec = {**SPEC, "milestones": None, "issues": [{"title": "a", "iid": 1}]}
    assert graph.cards_from_spec(spec, "https://gl")[0]["milestone_due"] is None


def test_empty_milestone_is_a_root_and_not_faded():
    g = graph.build([card(1)], [{"title": "Later", "due_date": "2026-10-10"}])
    assert g["nodes"]["m:Later"]["open"] is True
    assert g["critical"]["m:Later"] == ["m:Later"]
    text = _text(graph.render_tree(g, "2026-09-30"))
    assert "◆ Later  due 2026-10-10 · 10 days left · no cards yet" in text
    assert "no cards yet" in graph.render_mermaid(g)
    assert list(graph.subgraph(g, "Later")["nodes"]) == ["m:Later"]


def test_known_milestone_with_cards_keeps_its_count():
    g = graph.build([card(1, milestone="Beta")], [{"title": "Beta"}])
    assert "no cards yet" not in _text(graph.render_tree(g, "2026-09-30"))
