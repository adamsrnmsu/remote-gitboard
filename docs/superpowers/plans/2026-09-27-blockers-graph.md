# Blockers, priority and the milestone graph — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cards carry `blocked_by`, `milestone` and `priority::` plus a board
order that the lead and Claude edit through the YAML. `gitboard graph` draws
the chains that converge on milestones, `stats`/`digest` flag
contradictions, and the stats dump carries the data perch needs.

**Architecture:** Four pure modules do the thinking: `links.py` (refs, the
footer, the cycle check, and GitLab link read/sync), `order.py` (a three-way
merge of the order plus minimal moves), `graph.py` (the model, the flags, the
tree, Mermaid) and `graph_html.py` (a self-contained SVG page). `apply.py`,
`board.py`, `stats.py`/`mail.py` and `cli.py` wire them in. The existing
three-way merge (`issue_changes`) gains two fields, so plan and apply still
share one decision point.

**Tech Stack:** Python 3.13+, stdlib (`graphlib`, `bisect`, `html`),
python-gitlab, rich, typer, pytest, ruff. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-27-blockers-graph-design.md`. Read
it before your task.

## Global Constraints

- Run tests with `env -u FORCE_COLOR PYTHONPATH=src .venv/bin/pytest -q`.
  This shell sets `FORCE_COLOR=3`, which breaks 7 CLI tests; that bug is
  tracked separately as gb-6fl. Lint with `.venv/bin/ruff check src tests`
  and format with `.venv/bin/ruff format src tests`. **Never** use `make
  test` in this worktree (it tries to rebuild `.venv`), and never
  `pip install -e`.
- `.venv` is a symlink. **Never `git add -A` or `git add .`.** Add only the
  files your task names.
- No new dependencies. `graph.py`, `graph_html.py`, `order.py` and
  `stats.py` are stdlib only (stats already is). `links.py` may import
  `gitboard.client` and `gitlab.exceptions`, and nothing else from gitboard.
- **`stats` must not import `estimate`.** `graph` must not import `stats`,
  `apply`, `board` or `cli`. `links` must not import `apply`, `board` or
  `graph`.
- Tests never hit the network. Fake the API surface the way
  `tests/test_apply.py` and `tests/test_board.py` already do.
- Only `log.out()` writes stdout; everything else goes to stderr via
  `log.err()`.
- `apply` stays additive for issues and milestones: it never deletes or
  closes one. Removing a blocker **link** is allowed.
- The link type string is exactly `"is_blocked_by"`. The footer line format
  is exactly `Blocked by: #9, infra/platform#4`.
- Match the surrounding style: short docstrings that say *why*, and a
  `ponytail:` comment on a deliberate ceiling.
- Every task: claim its bead first (`bd update <id> --claim`, run from
  `/Users/ryanadams/Documents/git/remote-gitboard`) and close it at the end
  (`bd close <id> --reason "..."`). On a Dolt lock error, retry once, then
  report the id back.
- **Do not commit.** The orchestrator commits after each wave, because
  parallel agents share this worktree and one git index.

## The card shape (shared contract)

`board.fetch_history` returns card dicts. `graph.cards_from_spec` builds the
same shape from a YAML. `graph.py` and `stats.py` consume it. The fields
this plan adds are marked NEW:

```python
{
    "iid": 12,  # None for a spec card not yet on GitLab
    "title": "Wire up board reader",
    "state": "opened",  # or "closed"
    "labels": ["Doing", "priority::1"],
    "assignee": "alice",  # or None
    "due_date": "2026-10-20",  # or None
    "milestone": "Beta launch",  # or None
    "milestone_due": "2026-11-01",  # NEW, or None
    "priority": 1,  # NEW: lowest priority::N digit, or None
    "blocked_by": [  # NEW
        {"ref": "9", "state": "opened", "since": "2026-09-20T10:02:11Z"},
        {"ref": "infra/platform#4", "state": None, "since": None},
    ],
    "web_url": "https://…/-/issues/12",  # or None
    # plus whatever fetch_history already has (transitions, verdicts, notes…)
}
```

**Refs** are strings:
- `"9"` is a card in this project;
- `"grp/x#4"` is a card in another project;
- `"new:<title>"` is a spec card with no iid yet.

A blocker's `state` is one of:
- `"opened"`: not closed and not in `Done`;
- `"closed"`: closed, in `Done`, or a same-project card absent from the
  data, since every open issue is on the board;
- `None`: unknown, which in practice means an external card.

**Graph node keys:**
- `"12"` for a card, `"new:<title>"` for a new card, `"grp/x#4"` for an
  external card;
- `"m:<milestone title>"` for a milestone.

## Review Focus

1. **Pull, then plan, is empty** on a board with a cross-project blocker, a
   footer-only ref, a milestone and a manual order. If it is not, every plan
   shows phantom changes. Test in Task 5 (fields) and Task 8 (order).
2. **A hand-written spec touches nothing it does not name.** No
   `blocked_by` key means links are left alone. No `.base` means no
   reorder. No `milestone` key means the milestone is left alone. Tests in
   Tasks 5 and 8.
3. **An offline blocker that is closed** (a same-project iid missing from a
   pulled YAML) is drawn faded and raises no `blocked_*` or inversion flag.
   Test in Task 3.
4. **CE downgrade.** A created link that comes back as `relates_to` is
   deleted, and `apply` raises one `GitlabProblem`, not one per card. Test
   in Task 1.
5. **Hostile titles.** `<script>`, quotes, `#`, brackets and unicode in a
   title must not break Mermaid or inject into the HTML page. Tests in
   Tasks 3 and 4.

---

## Waves (dependency order; tasks within a wave run in parallel)

| Wave | Tasks | Files owned |
|---|---|---|
| 1 | T1 links, T2 order | `src/gitboard/links.py`, `tests/test_links.py`; `src/gitboard/order.py`, `tests/test_order.py` |
| 2 | T3 graph core, T5 apply fields, T6 board | `graph.py`, `tests/test_graph.py`; `apply.py`, `tests/test_apply.py`; `board.py`, `tests/test_board.py` |
| 3 | T4 html, T7 stats flags, T8 order in apply | `graph_html.py`, `tests/test_graph_html.py`; `stats.py`, `mail.py`, `tests/test_stats.py`, `tests/test_mail.py`; `apply.py`, `tests/test_apply.py` |
| 4 | T9 CLI + docs | `cli.py`, `tests/test_cli.py`, `.claude/commands/board.md`, `CLAUDE.md`, `docs/board-yaml.md`, `docs/commands.md` |

T8 edits `apply.py` after T5 is committed, and no other wave-3 task touches
that file.

---

### Task 1: `links.py` — refs, footer, cycle check, GitLab link read/sync

**Files:**
- Create: `src/gitboard/links.py`
- Test: `tests/test_links.py`

**Interfaces:**
- Consumes: `gitboard.client.GitlabProblem`, `gitlab.exceptions.GitlabError`
  (it has `.response_code`).
- Produces:
  - `BLOCKED_BY = "is_blocked_by"`, `PRIORITY = "priority::"`
  - `norm_ref(value, project_path: str, titles: dict[str, int | None]) -> str`,
    which raises `ValueError`
  - `norm_refs(values, project_path, titles) -> list[str]` (unique, sorted
    by `ref_key`)
  - `ref_key(ref: str) -> tuple`, the sort key: same-project refs by int,
    then external refs, then `new:` refs
  - `footer_refs(description: str | None, project_path: str) -> list[str]`
  - `check(spec: dict) -> None`, which raises `ValueError`
  - `priority(labels) -> int | None`
  - `read(issue, project) -> list[dict]`, returning
    `{"ref", "since", "link_id", "source"}` with `source` in
    `{"native", "footer"}`
  - `sync(issue, project, want: list[str], have: list[dict], project_id_of) -> list[tuple]`,
    which returns `("skipped", "link", detail)` records and raises
    `GitlabProblem` on a downgrade

- [ ] **Step 1: Write the failing tests**

```python
"""links.py: refs, the footer, the cycle check, and link read/sync on fakes."""

import types

import pytest
from gitlab.exceptions import GitlabListError

from gitboard import client, links

P = "grp/proj"


def test_norm_ref_forms():
    titles = {"Token rotation": 9, "Brand new": None}
    assert links.norm_ref(9, P, titles) == "9"
    assert links.norm_ref("9", P, titles) == "9"
    assert links.norm_ref("#9", P, titles) == "9"
    assert links.norm_ref("grp/proj#9", P, titles) == "9"
    assert links.norm_ref("infra/platform#4", P, titles) == "infra/platform#4"
    assert links.norm_ref("Token rotation", P, titles) == "9"
    assert links.norm_ref(" Brand new ", P, titles) == "new:Brand new"
    with pytest.raises(ValueError, match="unknown blocker"):
        links.norm_ref("No such card", P, titles)


def test_norm_refs_sorts_numerically_and_dedupes():
    got = links.norm_refs(
        [10, "grp/proj#9", 9, "a/b#1", "Brand new"], P, {"Brand new": None}
    )
    assert got == ["9", "10", "a/b#1", "new:Brand new"]


def test_footer_refs_reads_only_the_last_nonempty_line():
    body = "Some text\nBlocked by: #9, infra/platform#4, grp/proj#11\n\n"
    assert links.footer_refs(body, P) == ["9", "11", "infra/platform#4"]
    assert links.footer_refs("Blocked by: #9\nmore text", P) == []
    assert links.footer_refs(None, P) == []
    assert links.footer_refs("blocked BY:  #3", P) == ["3"]


def test_priority_is_the_lowest_digit():
    assert links.priority(["Doing", "priority::3"]) == 3
    assert links.priority(["priority::2", "priority::1"]) == 1
    assert links.priority(["priority::high"]) is None
    assert links.priority([]) is None


def spec(*issues):
    return {"project": P, "board": "b", "issues": list(issues)}


def test_check_accepts_iids_titles_and_externals():
    links.check(
        spec(
            {"title": "a", "iid": 1, "blocked_by": ["b", 3, "x/y#2"]},
            {"title": "b"},
        )
    )


def test_check_rejects_unknown_title():
    with pytest.raises(ValueError, match="'nope'"):
        links.check(spec({"title": "a", "blocked_by": ["nope"]}))


def test_check_rejects_self_block():
    with pytest.raises(ValueError, match="blocks itself"):
        links.check(spec({"title": "a", "iid": 1, "blocked_by": [1]}))


def test_check_rejects_a_cycle_through_iids_and_titles():
    with pytest.raises(ValueError, match="cycle"):
        links.check(
            spec(
                {"title": "a", "iid": 1, "blocked_by": ["b"]},
                {"title": "b", "iid": 2, "blocked_by": [1]},
            )
        )


# --- read / sync on fakes ----------------------------------------------------


class FakeLinks:
    def __init__(self, items=(), downgrade=False, error=None):
        self.items, self.downgrade, self.error = list(items), downgrade, error
        self.created, self.deleted = [], []

    def list(self, **_):
        if self.error:
            raise self.error
        return list(self.items)

    def create(self, data):
        self.created.append(data)
        kind = "relates_to" if self.downgrade else data["link_type"]
        self.items.append(
            link(
                data["target_issue_iid"],
                data["target_project_id"],
                kind,
                900 + len(self.created),
            )
        )
        return (None, None)

    def delete(self, link_id):
        self.deleted.append(link_id)
        self.items = [x for x in self.items if x.issue_link_id != link_id]


def link(iid, project_id, kind="is_blocked_by", link_id=1, full=None):
    return types.SimpleNamespace(
        iid=iid,
        project_id=project_id,
        link_type=kind,
        issue_link_id=link_id,
        link_created_at="2026-09-20T10:00:00Z",
        references={"full": full or f"other/x#{iid}"},
    )


PROJECT = types.SimpleNamespace(id=7, path_with_namespace=P)


def issue(items=(), description="", **kw):
    return types.SimpleNamespace(
        iid=12, title="t", description=description, links=FakeLinks(items, **kw)
    )


def test_read_keeps_only_is_blocked_by_and_maps_projects():
    i = issue(
        [
            link(9, 7),
            link(4, 8, full="infra/platform#4", link_id=2),
            link(5, 7, "relates_to", 3),
        ]
    )
    got = links.read(i, PROJECT)
    assert [(g["ref"], g["source"], g["link_id"]) for g in got] == [
        ("9", "native", 1),
        ("infra/platform#4", "native", 2),
    ]
    assert got[0]["since"] == "2026-09-20T10:00:00Z"


def test_read_unions_the_footer_native_wins():
    i = issue([link(9, 7)], description="x\nBlocked by: #9, #11")
    got = links.read(i, PROJECT)
    assert [(g["ref"], g["source"]) for g in got] == [("9", "native"), ("11", "footer")]


def test_read_falls_back_to_footer_on_403():
    err = GitlabListError("forbidden", response_code=403)
    i = issue(description="Blocked by: #3", error=err)
    assert [g["ref"] for g in links.read(i, PROJECT)] == ["3"]


def test_sync_adds_and_removes_native_links():
    i = issue([link(9, 7, link_id=1), link(4, 8, full="infra/platform#4", link_id=2)])
    have = links.read(i, PROJECT)
    records = links.sync(
        i, PROJECT, ["9", "10", "other/y#5"], have, lambda path: {"other/y": 55}[path]
    )
    assert i.links.deleted == [2]
    assert {
        (d["target_issue_iid"], d["target_project_id"]) for d in i.links.created
    } == {(10, 7), (5, 55)}
    assert all(d["link_type"] == "is_blocked_by" for d in i.links.created)
    assert records == []


def test_sync_never_duplicates_or_removes_a_footer_ref():
    i = issue(description="Blocked by: #9")
    have = links.read(i, PROJECT)
    assert links.sync(i, PROJECT, ["9"], have, None) == []
    assert i.links.created == []
    records = links.sync(i, PROJECT, [], have, None)
    assert records == [
        ("skipped", "link", "t: blocker #9 is a footer ref — edit the description")
    ]


def test_sync_downgrade_deletes_and_raises_once():
    i = issue(downgrade=True)
    with pytest.raises(client.GitlabProblem, match="Premium"):
        links.sync(i, PROJECT, ["9", "10"], [], None)
    assert i.links.items == []  # the relates_to links were deleted again
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `env -u FORCE_COLOR PYTHONPATH=src .venv/bin/pytest -q tests/test_links.py`
Expected: an ImportError / ModuleNotFoundError for `gitboard.links`.

- [ ] **Step 3: Implement `src/gitboard/links.py`**

```python
"""Blocker links: refs, the description footer, the cycle check, and the
GitLab read/sync. Pure except `read`/`sync`, which take API objects.

A ref is a string: "9" (this project), "grp/x#4" (another project), or
"new:<title>" (a spec card GitLab has not seen). Native `is_blocked_by`
links are what gets written; a `Blocked by: #9` last line is only read, so
a Free/CE dev instance (which downgrades `blocks` to `relates_to`) can still
show a graph.
"""

import graphlib
import re

from gitlab.exceptions import GitlabError

from gitboard import client
from gitboard.log import get_logger

log = get_logger()

BLOCKED_BY = "is_blocked_by"
PRIORITY = "priority::"
FOOTER = re.compile(r"^\s*blocked by:\s*(.+?)\s*$", re.IGNORECASE)
REF = re.compile(r"^(?:(?P<path>[\w.\-/]+))?#(?P<iid>\d+)$")
_warned = set()


def ref_key(ref):
    if ref.isdigit():
        return (0, int(ref), "")
    if ref.startswith("new:"):
        return (2, 0, ref)
    return (1, 0, ref)


def norm_ref(value, project_path, titles):
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if text.isdigit():
        return text
    if m := REF.match(text):
        path = m["path"]
        return m["iid"] if path in (None, project_path) else f"{path}#{m['iid']}"
    if text in titles:
        iid = titles[text]
        return str(iid) if iid is not None else f"new:{text}"
    raise ValueError(
        f"unknown blocker {text!r}: not an iid, a group/project#iid, or a card title"
    )


def norm_refs(values, project_path, titles):
    return sorted(
        {norm_ref(v, project_path, titles) for v in values or []}, key=ref_key
    )


def footer_refs(description, project_path):
    lines = [
        x for x in (description or "").replace("\r\n", "\n").split("\n") if x.strip()
    ]
    if not lines or not (m := FOOTER.match(lines[-1])):
        return []
    refs = [x.strip() for x in m[1].split(",") if x.strip()]
    return norm_refs([r for r in refs if REF.match(r)], project_path, {})


def priority(labels):
    digits = [
        int(x[len(PRIORITY) :])
        for x in labels or []
        if x.startswith(PRIORITY) and x[len(PRIORITY) :].isdigit()
    ]
    return min(digits) if digits else None


def check(spec):
    """Unknown title refs, self-blocks and cycles among the spec's cards."""
    project = spec["project"]
    titles = {i["title"].strip(): i.get("iid") for i in spec["issues"]}
    key_of = {
        t: (str(iid) if iid is not None else f"new:{t}") for t, iid in titles.items()
    }
    graph = {}
    for i in spec["issues"]:
        if "blocked_by" not in i:
            continue
        me = key_of[i["title"].strip()]
        refs = norm_refs(i["blocked_by"], project, titles)
        if me in refs:
            raise ValueError(f"{i['title']!r} blocks itself")
        graph[me] = set(refs)
    try:
        tuple(graphlib.TopologicalSorter(graph).static_order())
    except graphlib.CycleError as e:
        raise ValueError(f"blocked_by cycle: {' -> '.join(e.args[1])}") from e


def _ref_of(link, project):
    if link.project_id == project.id:
        return str(link.iid)
    full = (getattr(link, "references", None) or {}).get("full")
    if full:
        return norm_ref(full, project.path_with_namespace, {})
    path = link.web_url.split("/-/issues/")[0].split("://", 1)[-1].split("/", 1)[1]
    return f"{path}#{link.iid}"


def read(issue, project):
    """Native is_blocked_by links, then footer refs not already linked."""
    path = project.path_with_namespace
    try:
        native = [
            {
                "ref": _ref_of(x, project),
                "since": getattr(x, "link_created_at", None),
                "link_id": x.issue_link_id,
                "source": "native",
            }
            for x in issue.links.list(all=True)
            if getattr(x, "link_type", None) == BLOCKED_BY
        ]
    except GitlabError as e:
        if getattr(e, "response_code", None) not in (403, 404):
            raise
        if path not in _warned:
            _warned.add(path)
            log.warning(
                "%s: issue links unavailable (%s); reading footer refs only",
                path,
                e.response_code,
            )
        native = []
    seen = {x["ref"] for x in native}
    footer = [
        {"ref": r, "since": None, "link_id": None, "source": "footer"}
        for r in footer_refs(getattr(issue, "description", None), path)
        if r not in seen
    ]
    return sorted(native, key=lambda x: ref_key(x["ref"])) + footer


def sync(issue, project, want, have, project_id_of):
    """Make the native links match `want` (refs, no "new:"). Footer refs are
    never duplicated as links and never removed (that is a description edit)."""
    records = []
    have_refs = {x["ref"]: x for x in have}
    for ref, x in have_refs.items():
        if ref in want:
            continue
        if x["source"] == "footer":
            shown = f"#{ref}" if ref.isdigit() else ref
            records.append(
                (
                    "skipped",
                    "link",
                    f"{issue.title}: blocker {shown} is a footer ref — edit the description",
                )
            )
        else:
            issue.links.delete(x["link_id"])
    added = []
    for ref in want:
        if ref in have_refs:
            continue
        if ref.isdigit():
            target_project, iid = project.id, int(ref)
        else:
            path, iid = ref.rsplit("#", 1)
            target_project, iid = project_id_of(path), int(iid)
        issue.links.create(
            {
                "target_project_id": target_project,
                "target_issue_iid": iid,
                "link_type": BLOCKED_BY,
            }
        )
        added.append((target_project, iid))
    if added:
        wrong = [
            x
            for x in issue.links.list(all=True)
            if (x.project_id, x.iid) in added and x.link_type != BLOCKED_BY
        ]
        for x in wrong:
            issue.links.delete(x.issue_link_id)
        if wrong:
            raise client.GitlabProblem(
                "blocking links need GitLab Premium: this instance made a "
                f"{wrong[0].link_type} link instead (removed again)"
            )
    return records
```

Adjust until the tests pass. Keep `check`'s messages quoting the offending
title with `!r`.

- [ ] **Step 4: Run the tests to confirm they pass**

Run: `env -u FORCE_COLOR PYTHONPATH=src .venv/bin/pytest -q tests/test_links.py && .venv/bin/ruff check src tests`
Expected: PASS, and ruff clean. Also run the full suite and expect 314+
passed.

- [ ] **Step 5: Close the bead.** Do not commit; the orchestrator commits.

---

### Task 2: `order.py` — the three-way order merge and minimal moves

**Files:**
- Create: `src/gitboard/order.py`
- Test: `tests/test_order.py`

**Interfaces:**
- Produces:
  - `merge(edited: list[int], live: list[int], old: list[int] | None) -> str | None`.
    Returns `"write"`, `"skipped"`, `"drift"` or `None` (nothing to do or
    unmanaged). All three lists are restricted to the iids common to all of
    them before comparing.
  - `moves(wanted: list[int], live: list[int]) -> list[tuple[int, int | None, int | None]]`.
    Returns `(iid, after_iid, before_iid)` over the iids common to both
    lists. Applied in order, the moves turn `live` into `wanted`.
    `after_iid` is `None` only for a card moving to the front; then
    `before_iid` names the first kept card.
  - `apply_moves(order: list[int], moves) -> list[int]`: pure, for tests and
    plan text.

- [ ] **Step 1: Write the failing tests**

```python
"""order.py: three-way order merge and the fewest reorder calls."""

import itertools

from gitboard import order


def test_merge_unmanaged_without_base():
    assert order.merge([2, 1], [1, 2], None) is None


def test_merge_cases():
    assert order.merge([1, 2, 3], [1, 2, 3], [1, 2, 3]) is None
    assert order.merge([1, 2, 3], [3, 2, 1], [1, 2, 3]) == "skipped"
    assert order.merge([3, 2, 1], [1, 2, 3], [1, 2, 3]) == "write"
    assert order.merge([3, 2, 1], [3, 2, 1], [1, 2, 3]) is None
    assert order.merge([3, 1, 2], [2, 1, 3], [1, 2, 3]) == "drift"


def test_merge_compares_only_common_iids():
    # 4 is new in the YAML, 5 appeared on GitLab: neither is an order change
    assert order.merge([1, 4, 2], [5, 1, 2], [1, 2]) is None


def test_moves_keep_the_longest_run():
    got = order.moves([1, 2, 3, 4, 5], [2, 3, 4, 5, 1])
    assert got == [(1, None, 2)]


def test_moves_after_predecessor():
    assert order.moves([1, 3, 2], [1, 2, 3]) == [(3, 1, None)]


def test_moves_reach_every_permutation():
    base = [1, 2, 3, 4, 5]
    for wanted in itertools.permutations(base):
        got = order.moves(list(wanted), base)
        assert order.apply_moves(base, got) == list(wanted)
        assert len(got) == len(base) - _lis(list(wanted), base)


def _lis(wanted, live):
    pos = [live.index(x) for x in wanted]
    best = [1] * len(pos)
    for i in range(len(pos)):
        for j in range(i):
            if pos[j] < pos[i]:
                best[i] = max(best[i], best[j] + 1)
    return max(best, default=0)


def test_moves_ignore_cards_not_in_both():
    got = order.moves([9, 2, 1], [1, 2, 8])
    assert got == [(2, None, 1)]
    assert order.apply_moves([1, 2], got) == [2, 1]
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `env -u FORCE_COLOR PYTHONPATH=src .venv/bin/pytest -q tests/test_order.py`
Expected: an ImportError.

- [ ] **Step 3: Implement `src/gitboard/order.py`**

```python
"""Board order: GitLab keeps one manual order per project (relative_position)
and every list shows it. The YAML's issue order stands for it. Pure.

Only iids present on every side are compared: a card new in the YAML or new
on GitLab is not an order change, and lands wherever GitLab puts it.
"""

from bisect import bisect_left


def _common(*seqs):
    keep = set(seqs[0]).intersection(*seqs[1:])
    return [[x for x in s if x in keep] for s in seqs]


def merge(edited, live, old):
    if old is None:
        return None
    edited, live, old = _common(edited, live, old)
    if edited == old:
        return "skipped" if live != old else None
    if live == edited:
        return None
    return "write" if live == old else "drift"


def _kept(wanted, live):
    """The longest run of `wanted` already in `live` order (patience LIS)."""
    pos = {x: i for i, x in enumerate(live)}
    seq = [pos[x] for x in wanted]
    tails, tails_at, prev = [], [], [None] * len(seq)
    for i, p in enumerate(seq):
        k = bisect_left(tails, p)
        if k == len(tails):
            tails.append(p)
            tails_at.append(i)
        else:
            tails[k], tails_at[k] = p, i
        prev[i] = tails_at[k - 1] if k else None
    kept, i = set(), tails_at[-1] if tails_at else None
    while i is not None:
        kept.add(wanted[i])
        i = prev[i]
    return kept


def moves(wanted, live):
    wanted, live = _common(wanted, live)
    kept = _kept(wanted, live)
    first_kept = next((x for x in wanted if x in kept), None)
    out = []
    for at, iid in enumerate(wanted):
        if iid in kept:
            continue
        after = wanted[at - 1] if at else None
        out.append((iid, after, first_kept if after is None else None))
    return out


def apply_moves(order, moves_):
    order = list(order)
    for iid, after, before in moves_:
        if iid not in order:
            continue
        order.remove(iid)
        at = order.index(after) + 1 if after is not None else order.index(before)
        order.insert(at, iid)
    return order
```

Moves are emitted in `wanted` order, so each card's predecessor is already
in place when it moves. `test_moves_reach_every_permutation` is the proof.

- [ ] **Step 4: Run the tests to confirm they pass.** Then run the full
  suite and ruff.
- [ ] **Step 5: Close the bead.** Do not commit.

---

### Task 3: `graph.py` — the model, the flags, `cards_from_spec`, the tree, Mermaid

**Files:**
- Create: `src/gitboard/graph.py`
- Test: `tests/test_graph.py`

**Interfaces:**
- Consumes: `links.norm_refs`, `links.ref_key`, `links.priority` (Task 1);
  the card shape above.
- Produces:
  - `DONE, FAILED, VERIFY, BLOCKED = "Done", "Failed", "Verify", "Blocked"`
  - `is_open(card) -> bool`: `state == "opened"` and `Done` not in labels.
  - `cards_from_spec(spec: dict, url: str) -> list[dict]`
  - `build(cards: list[dict]) -> dict`, with keys:
    - `nodes`: `{key: node}`
    - `edges`: `[(src, dst)]`
    - `downstream`: `{key: int}`
    - `critical`: `{milestone_key: [keys…, milestone_key]}`
    - `layers`: `{key: (layer, pos)}`

    A node is `{"key", "kind": "card"|"external"|"milestone", "iid",
    "title", "open": bool, "assignee", "labels", "due_date", "priority",
    "milestone", "web_url"}`. A milestone node has `title`, `due_date` and
    `open` (True while any of its cards is open).
  - `subgraph(g, milestone_title) -> dict`: the same shape, keeping only the
    milestone and everything upstream of it.
  - `flags(cards: list[dict], columns: list[str]) -> dict[str, list[dict]]`.
    Its keys are `blocked_stale`, `blocked_unmarked`, `priority_inversion`,
    `date_inversion` and `unowned_blocker`. Each item is `{"iid", "title",
    "assignee", "url", "blocker": ref | None, "detail": str}`.
  - `render_tree(g, today: str) -> rich.console.Group`
  - `render_mermaid(g) -> str`

**Rules:**
- **Edges.** For every card, one `blocker_key → card_key` edge per
  `blocked_by` ref, and one `card_key → "m:<milestone>"` edge if the card
  has a milestone. A blocker ref not among the cards becomes a node with
  `kind="external"`. For a same-project ref that is absent (so closed), the
  node is `kind="card"` with `open=False` and `title=f"#{iid}"`. An
  external node's `open` is `state != "closed"` from the ref entry.
- **`downstream[k]`:** the number of distinct *card or external* nodes
  reachable from `k` along edges, not counting milestones.
- **`critical[m]`:** the longest path, in edges, that ends at `m`. Compute
  `depth[n] = 0` for nodes with no incoming edge, else `1 + max(depth[p])`.
  Take the predecessor with the maximum depth, breaking ties by the lowest
  `links.ref_key`. Walk back from `m` through those predecessors.
- **`layers`:** `layer = depth[n]`, and every milestone gets
  `max(depth of non-milestones) + 1`. Start each layer sorted by
  `ref_key`. Then do two sweeps of barycenter ordering: layer 1 upward by
  the mean position of predecessors, then back down by the mean position of
  successors. A node with no neighbours in the sweep keeps its position,
  and ties are broken by `ref_key`. `pos` is the index within the layer.
- **`flags`** (cards are the dicts passed in; "open" uses `is_open`;
  `rank(p) = p if p is not None else 5`; a card's date is `due_date or
  milestone_due`). Each flag applies only to open cards:
  - `blocked_stale`: the card is in `Blocked`, and no blocker is open
    (external `state None` counts as open). Only when `BLOCKED` is in
    `columns` (case-insensitive).
  - `blocked_unmarked`: the card has an open blocker, and it is not in
    Blocked, Verify, Done or Failed. Only when Blocked is in `columns`.
  - `priority_inversion`: a same-project blocker card is open and
    `rank(blocker) > rank(card)`. Detail:
    `"#12 (P1) waits on #9 (P3)"`, using `P–` for none.
  - `date_inversion`: a same-project blocker card is open, both dates are
    set, and the blocker's date is later than the card's. Detail:
    `"#12 due 2026-10-20 waits on #9 due 2026-11-02"`.
  - `unowned_blocker`: a same-project blocker card is open and unassigned,
    and the blocked card has a milestone. Emit it once per (blocker,
    blocked) pair, with `iid` being the **blocker**, `blocker` the blocked
    card's ref, and detail `"#9 (unassigned) blocks #12 in Beta launch"`.
  - Lists are sorted by `(iid, blocker)`.
- **`cards_from_spec`:**
  - `titles = {title: iid}`. Each spec issue becomes one card:
    `state="opened"`, `priority=links.priority(labels)`, `milestone`, and
    `milestone_due` from `spec["milestones"]` (ISO string; dates coerced
    with `.isoformat()`).
  - `blocked_by` holds refs normalised with `links.norm_refs`, each with a
    state: a same-project ref whose iid is in the spec is `"opened"` unless
    that card has the `Done` label (then `"closed"`); a same-project ref not
    in the spec is `"closed"`; a `new:` ref is `"opened"`; an external ref
    is `None`. `since` is None.
  - `web_url` follows the pattern in `board.columns_from_spec`.
  - A card without an iid gets `iid=None`, and its key is `new:<title>`.
- **`render_tree`:**
  - One root per milestone, sorted by `(due_date or "9999", title)`. The
    root text is `"◆ <title>  due <date> · <n> days left · <d>/<t> done"`
    (omit the parts that do not apply; days can be negative, in which case
    print `<n> days late`).
  - The root's children are that milestone's cards that are not a blocker
    of another card in the same milestone. Recurse into each node's
    blockers, sorted by `ref_key`.
  - A node already printed anywhere prints once more as `"(see #9 above)"`
    and is not expanded.
  - After the milestones comes a final root `"No milestone"`, holding cards
    without a milestone that have any edge and were not printed; its tops
    are those with no unprinted dependents.
  - A card line reads `"#12 Title  @alice  P1  due 2026-10-20  ⇠ 3 waiting
    ★"`. Parts that are absent are omitted. The `★` appears when the node
    is in any `critical` path. A closed node is styled `muted`; an open
    node in a flagged pair is marked with a trailing `  ⚑`.
  - Return `Group(*trees)`.
- **`render_mermaid`:**
  - The first line is `flowchart LR`.
  - Node ids: a card is `i<iid>`. A new card is `n_<slug(title)>`, an
    external card is `x_<slug(ref)>`, and a milestone is
    `m_<slug(title)>`. `slug` lowercases and turns each non-`[a-z0-9]` run
    into `_`, so `"m:Beta"` becomes `m_beta` and `x/y#7` becomes
    `x_x_y_7`.
  - Labels: `i12["#35;12 Title"]`. In labels, replace `"` with `#quot;`, `#`
    with `#35;`, `<` with `#lt;` and `>` with `#gt;`. Replace `#` first,
    before the other substitutions add `#` characters.
  - A milestone is a stadium, `m_beta(["Beta launch · due 2026-11-01"])`.
  - Edges are `i9 --> i12`. Closed nodes are listed in `class i9,i4 done;`,
    and `classDef done fill:#eeeeee,color:#888888;` is emitted when any
    node is closed.

- [ ] **Step 1: Write the failing tests** (`tests/test_graph.py`):

```python
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
```

- [ ] **Step 2: Run the tests to confirm they fail**

Run: `env -u FORCE_COLOR PYTHONPATH=src .venv/bin/pytest -q tests/test_graph.py`
Expected: an ImportError.

- [ ] **Step 3: Implement `graph.py`** to the rules above. Keep each
  function short. Use `collections.defaultdict` for adjacency and
  `graphlib.TopologicalSorter` for depth order (an input that is not a DAG
  cannot occur after `links.check`; if a live board has a cycle made in the
  GitLab UI, catch `CycleError` and drop the back-edge, with a `ponytail:`
  comment). The module docstring states that the card shape is
  `board.fetch_history`'s.

- [ ] **Step 4: Run the tests to confirm they pass.** Then run the full
  suite and ruff.
- [ ] **Step 5: Close the bead.** Do not commit.

---

### Task 4: `graph_html.py` — a self-contained interactive page

**Files:**
- Create: `src/gitboard/graph_html.py`
- Test: `tests/test_graph_html.py`

**Interfaces:**
- Consumes: `graph.build` output (Task 3); `mail.COLUMN_COLORS`.
- Produces: `render_html(g: dict, title: str, flagged: set[str] = frozenset()) -> str`.

**Rules:**
- One HTML5 document. It has a `<title>`, a `<style>` with light tokens
  plus a `@media (prefers-color-scheme: dark)` override, one `<svg>`, and
  one inline `<script>`. There are no external URLs anywhere except each
  node's own `web_url` in an `<a href>`.
- **Positions:** `x = 24 + layer * 240`, `y = 24 + pos * 72`. Card nodes are
  `rect` 200×52 with rx=8; milestone nodes are 220×64 with rx=32 and bold.
  The svg `width` and `height` fit the maximum extent plus 24.
- **Edges:** a cubic `path` from the right edge of the source to the left
  edge of the target, with an arrow marker (`<marker id="arrow">`).
- **Styling:**
  - Node fill uses `COLUMN_COLORS` for the card's first label that is a
    column there (default `#7f8ea3`) at 18% opacity, with a 2px stroke in
    that colour.
  - Closed nodes get `class="closed"` (opacity .45).
  - Nodes in `flagged` get `class="flag"` (red stroke `#d03b3b`), and so do
    edges whose target is in `flagged`.
  - Nodes on any critical path get `class="crit"` (stroke width 3).
- **Text:** `#12 Title`, truncated to 28 characters with `…`, plus a second
  smaller line `@alice · P1 · due 2026-10-20`. The hover tooltip is a
  native `<title>` child holding the full title, assignee, column labels,
  priority and due date. Hover needs no JS.
- **JS** (about 50 lines): embed `const UP = {...}, DOWN = {...}` adjacency
  as JSON (escape `</` as `<\/`).
  - Clicking a node's `g[data-key]` adds `dim` to every node and edge, then
    removes `dim` and adds `hl` for the clicked node's transitive upstream,
    its downstream, and the edges between them.
  - Clicking the svg background clears the highlight.
  - Links open normally with ctrl/cmd-click; a plain click on a node
    highlights instead of navigating.
- Every piece of text goes through `html.escape(..., quote=True)`.

- [ ] **Step 1: Write the failing tests**

```python
"""graph_html.py: one self-contained page; no external fetches; escaped."""

from html.parser import HTMLParser

from gitboard import graph, graph_html
from tests.test_graph import CARDS, card


class Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags, self.srcs, self.hrefs = [], [], []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        a = dict(attrs)
        if "src" in a:
            self.srcs.append(a["src"])
        if tag == "link" and "href" in a:
            self.hrefs.append(a["href"])


def test_page_is_self_contained():
    page = graph_html.render_html(graph.build(CARDS), "grp/proj — Dev")
    s = Scan()
    s.feed(page)
    assert s.tags.count("svg") == 1 and "script" in s.tags
    assert s.srcs == [] and s.hrefs == []
    assert "prefers-color-scheme: dark" in page
    assert 'data-key="m:Beta"' in page


def test_titles_are_escaped():
    cards = [
        card(1, '<script>alert(1)</script> & "q"'),
        card(2, blocked_by=[("1", "opened")], milestone="Beta"),
    ]
    page = graph_html.render_html(graph.build(cards), "t")
    assert "<script>alert(1)" not in page
    assert "&lt;script&gt;" in page


def test_flagged_and_closed_classes():
    cards = [
        card(1, state="closed"),
        card(2, blocked_by=[("1", "closed")], milestone="Beta"),
    ]
    page = graph_html.render_html(graph.build(cards), "t", flagged={"2"})
    assert 'class="node closed' in page and "flag" in page
```

If `tests/test_graph.py` cannot be imported as `tests.test_graph` (check
whether `tests/__init__.py` exists), copy the `card` helper and `CARDS` into
this test file instead.

- [ ] **Step 2: Run the tests; expect an ImportError.**
- [ ] **Step 3: Implement it to the rules.**
- [ ] **Step 4: Run the tests to confirm they pass.** Then write
  `$CLAUDE_JOB_DIR/tmp/graph.html` (or `/tmp/gb-graph.html` if that is
  unset) from `CARDS` and open it once in a check that the file exists and
  is non-empty. Run the full suite and ruff.
- [ ] **Step 5: Close the bead.** Do not commit.

---

### Task 5: `apply.py` — milestone and blocked_by fields, milestones, link sync, pull

**Files:**
- Modify: `src/gitboard/apply.py`. The functions touched are `load`,
  `issue_entry`, `spec_from_board`, `wanted_issue`, `current_issue`,
  `issue_changes`, `ensure_issues`, `apply`, `plan`, `have_from_spec`,
  `diff` and `_payload`. It adds `ensure_milestones`, `milestone_changes`
  and `_project_id_of`.
- Test: `tests/test_apply.py` (extend `FakeIssue`/`FakeProject` with
  `links`, `milestone`, `relative_position` and `id`, and `FakeProject`
  with `milestones`, `id` and `namespace`).

**Interfaces:**
- Consumes: from Task 1, `links.check`, `links.norm_refs`, `links.read`,
  `links.sync`, `links.priority`, `links.PRIORITY` and `links.ref_key`.
- Produces:
  - `FIELDS = ("labels", "description", "due_date", "assignee", "milestone", "blocked_by")`
  - `wanted_issue(spec_i, project_path=None, titles=None) -> dict`. It
    includes `milestone` only if the key is present (value `str | None`),
    and `blocked_by` only if the key is present (normalised refs, which may
    include `new:`).
  - `current_issue(issue, blocked_by=None) -> dict`. It always includes
    `milestone` (the title or None), and includes `blocked_by` only when
    that argument is given.
  - `issue_changes` iterates `[f for f in FIELDS if f in edited]`. When
    `old` exists but lacks the field, the default is `[]` for `blocked_by`
    and `None` for `milestone`. `_show` renders a `blocked_by` list as
    `[#9, infra/x#4, new:T]`.
  - `ensure_milestones(gl, project, spec, record) -> {title: id}`
  - `milestone_changes(spec, have) -> list[tuple]`, the pure form used by
    `diff`. `have["milestones"]` is
    `{title: {"due_date": str | None, "description": str}}`.

**Behaviour** (spec §B; implement it test-first, one test per bullet):

1. **`load`:**
   - `links.check(spec)` → `SpecError(f"{path}: {e}")`;
   - a `milestone:` not in `milestones:` →
     `SpecError(f"{path}: milestone 'X' (issue 't') is not under milestones:")`;
   - two `priority::` labels on one card → `SpecError`;
   - `milestones:` entries need a `title`, and duplicates are rejected.
2. **Normalisation:** `wanted_issue` normalises `blocked_by` with
   `titles = {title: iid}` of the whole spec, passed by `diff`,
   `ensure_issues` and `have_from_spec`. A `milestones` due date that is a
   date object is coerced to ISO in `milestone_changes` and
   `ensure_milestones`.
3. **`plan`:**
   - For spec issues that **have a `blocked_by` key** and are found live,
     fetch `links.read(issue, project)` and pass the refs into
     `current_issue`. Other issues cost no links request.
   - `have["milestones"]` comes from `project.milestones.list(all=True)`
     plus group milestones when `project.namespace["kind"] == "group"`
     (`gl.groups.get(project.namespace["full_path"]).milestones.list(all=True)`).
     Wrap that in try/except `GitlabError`, falling back to the project
     milestones only.
4. **`diff`:**
   - Emits `milestone_changes` (`("added", "milestone", title)`,
     `("changed", "milestone", f"{title}: due_date a -> b")`,
     `("changed", "milestone", f"{title}: description (edited)")`) right
     after the label changes.
   - Any `new:` ref in a wanted `blocked_by` makes the field differ, so it
     is reported through the normal `issue_changes` record.
   - With `have is None` (a fresh project), add `("added", "milestone", t)`
     for each milestone.
5. **`have_from_spec(base)`:**
   - Its `issues` are `wanted_issue(i, base["project"], titles)`, so a
     base card lacking the key lacks the field.
   - Its `milestones` come from `base["milestones"]`.
6. **`ensure_issues`:**
   - Resolve milestone ids from the map `ensure_milestones` returns;
     `_payload(fields, users, milestones)` turns `milestone` into
     `milestone_id` (None when cleared).
   - `blocked_by` is popped from the save payload.
   - After all creates and saves, a second pass over spec issues with a
     `blocked_by` key does the linking. It resolves `new:<title>` via the
     returned `live` map to `str(live[title].iid)`, calls
     `links.sync(issue, project, want, links.read(issue, project),
     project_id_of)`, and records what it returns.
   - `project_id_of(path)` is `_project_id_of(gl)`, a cached
     `client.get_project(gl, path).id`.
   - A 404 or GitlabGetError on a target is recorded as
     `("skipped", "link", f"{title}: blocker {ref} not found")`, and the
     other links still apply.
   - The link pass runs only for issues whose `issue_changes` put
     `blocked_by` in `writes`. That is how three-way skipped/drift is
     honoured.
7. **`apply()` order:** `resolve_users` → `ensure_project` → labels →
   board → `ensure_milestones` → `ensure_issues` (links inside) → notes.
8. **`issue_entry(issue, blocked_by=None)`:**
   - Adds `milestone: <title>` when the issue has one.
   - Adds `blocked_by` when given a non-empty list: refs as written by pull,
     where same-project refs are written as **ints** and external refs as
     strings.
9. **`spec_from_board`:**
   - Calls `links.read(issue, project)` for each issue and passes the refs.
   - Adds `milestones:` (sorted by due date then title) for every milestone
     a card carries, with `title`, `due_date` if set, and `description`
     (via `norm_text`) if non-empty. The metadata comes from
     `issue.milestone` (a dict with `title`, `due_date`, `description`).
10. **Round trip:** `spec_from_board` → `load` → `diff` against the same
    live objects is `[]`. Build the fake with:
    - a native same-project link;
    - a cross-project link;
    - a footer-only ref;
    - a milestone with a due date;
    - a `priority::2` label.

**Required tests** (names are prescriptive; write the fake extensions
first):

```python
def test_load_rejects_blocked_by_cycle(tmp_path): ...
def test_load_rejects_unknown_milestone(tmp_path): ...
def test_load_rejects_two_priorities(tmp_path): ...
def test_absent_blocked_by_is_unmanaged(monkeypatch):
    ...
    # live issue has links, spec entry has no blocked_by key -> diff == [] and apply calls no links.create/delete


def test_empty_blocked_by_removes_native_links(monkeypatch): ...
def test_blocked_by_three_way_skipped_and_drift():
    ...
    # issue_changes: base [9], live [9, 11], yaml [9] -> skipped; base [9], live [11], yaml [10] -> drift


def test_new_title_ref_links_after_create(monkeypatch):
    ...
    # yaml: A blocked_by ["B"], B has no iid -> apply creates B, then A.links.create(target_issue_iid=B.iid)


def test_milestone_created_and_assigned(monkeypatch): ...
def test_milestone_clear_with_null(monkeypatch): ...
def test_plan_reports_milestone_changes(monkeypatch): ...
def test_group_milestone_counts_as_existing(monkeypatch): ...
def test_pull_then_plan_is_empty_with_links_milestones_priority(monkeypatch): ...
def test_blocker_not_found_is_skipped_and_rest_applies(monkeypatch): ...
def test_downgrade_raises_one_problem(monkeypatch): ...
```

- [ ] **Step 1:** Extend the fakes and write the tests above. Run them and
  confirm they fail.
- [ ] **Step 2:** Implement the behaviour, one bullet at a time, rerunning
  the relevant tests after each.
- [ ] **Step 3:** Run the full suite and ruff. Existing tests must still
  pass. The only existing expectation allowed to change is one that
  asserted the exact `current_issue` dict, which now also carries
  `milestone`. Fix such tests by adding `"milestone": None`, not by
  weakening them.
- [ ] **Step 4: Close the bead.** Do not commit.

---

### Task 6: `board.py` — history fields and the sort order

**Files:**
- Modify: `src/gitboard/board.py` (`fetch_history`, `_urgency`,
  `columns_from_spec`)
- Test: `tests/test_board.py`

**Interfaces:**
- Consumes: `links.read` and `links.priority` (Task 1).
- Produces:
  - each history dict gains `milestone_due`, `priority` and `blocked_by`
    (the card shape above);
  - `_urgency(issue) -> tuple`, which is overdue first, then
    `relative_position` (nulls last), then newest.

**Behaviour:**

1. **`fetch_history`:**
   - After building `seen`, for each issue call
     `links.read(issue, project)`. Build
     `blocked_by = [{"ref", "state", "since"}]`.
   - The state for a digit ref comes from `seen`: `"opened"` if that
     issue's state is opened and `"Done"` is not in its labels, else
     `"closed"`. A digit ref not in `seen` is `"closed"`, and an external
     ref is `None`.
   - `priority = links.priority(labels)`, and
     `milestone_due = milestone.get("due_date") if milestone else None`.
   - Update the docstring's request count: three requests per issue.
2. **`_urgency`:**
   - The key is `(not is_overdue(i), rp is None, rp or 0, -(i.iid or 0))`,
     with `rp = getattr(i, "relative_position", None)`.
   - Update its docstring: board order is GitLab's manual order, which the
     lead sets through the YAML, and overdue still surfaces first so a
     truncated column shows it.
3. **`columns_from_spec`:** each stand-in gets
   `relative_position=index` (the YAML list index) and `milestone` (a dict
   `{"title": …}` or None), so the YAML order is the board order offline.
4. `board_columns` is unchanged apart from the sort key.

**Required tests:**

```python
def test_columns_follow_board_order_after_overdue():
    ...
    # three issues: rp 3, rp 1, rp None; one overdue with rp 9 -> [overdue, rp1, rp3, None]


def test_columns_from_spec_use_yaml_order(): ...
def test_history_carries_blocked_by_priority_and_milestone_due():
    ...
    # HistIssue gains links (FakeLinks-like) + milestone {"title","due_date"}; blocker #9 open -> "opened",
    # blocker #8 closed in seen -> "closed", #77 not in seen -> "closed", other/x#4 -> None
```

Update `test_columns_are_ordered_deterministically` and
`test_columns_put_overdue_first` to the new key. Keep their intent; do not
delete them.

- [ ] **Steps:** write the tests, confirm they fail, implement, confirm they
  pass, then run the full suite and ruff. **Close the bead.** Do not
  commit.

---

### Task 7: stats and digest flags

**Files:**
- Modify: `src/gitboard/stats.py` (`summarise`, `for_person`,
  `three_moves`, `stat_row`, the team and person markdown renderers) and
  `src/gitboard/mail.py` (the team and person HTML)
- Test: `tests/test_stats.py`, `tests/test_mail.py`

**Interfaces:**
- Consumes: `graph.flags(cards, columns)` (Task 3). `stats` imports
  `graph`, which is allowed; it must not import `estimate`.
- Produces:
  - `summary["flow"]` gains `blocked_stale`, `blocked_unmarked`,
    `priority_inversion`, `date_inversion` and `unowned_blocker` (the lists
    from `graph.flags(history, columns)`);
  - `for_person` gains the same five keys, each filtered to items whose
    `assignee == username`;
  - `stat_row` gains the five counts;
  - `three_moves` adds an `"Unblock"` bucket right after `overdue`, built
    from the person's `unowned_blocker` and `priority_inversion` items, with
    `age = item["detail"]`.

**Behaviour:**
- `graph.flags` reads `c.get("blocked_by") or []` and `c.get("priority")`,
  so an old dump without those keys yields no flags. Add a test for that;
  it is the offline `--from` path on a dump written before this change.
- **Markdown:** one `### Blockers` section in the team page (and
  `## Your blockers` in the person page), shown only when any list is
  non-empty. It is a table `kind | card | detail`, following the pattern of
  `_tight_table`.
- **Mail:** a `_blockers(items, who=False)` block following the pattern of
  `_tight`, titled "Blockers the board disagrees with", placed after
  `_tight` in both the team and person zones.
  - Every `td` carries `bgcolor` and every text run a `color`;
    `tests/test_mail.py` already enforces this.

**Required tests:**

```python
def test_summarise_flags_blockers(): ...  # history with Blocked column + cards like test_graph.test_flags
def test_old_dump_without_blocked_by_has_no_flags(): ...
def test_for_person_filters_flags(): ...
def test_three_moves_includes_unblock_after_overdue(): ...
def test_stat_row_counts_flags(): ...
def test_team_markdown_has_blockers_section_only_when_flagged(): ...
def test_mail_blockers_block_renders_and_is_outlook_safe(): ...
```

- [ ] **Steps:** tests first, then implement, then the full suite and ruff.
  **Close the bead.** Do not commit.

---

### Task 8: manual order in `apply.py` and `pull`

**Files:**
- Modify: `src/gitboard/apply.py` (`spec_from_board`, `plan`,
  `have_from_spec`, `diff`, `apply`; add `ensure_order`)
- Test: `tests/test_apply.py`

**Interfaces:**
- Consumes: `order.merge`, `order.moves` and `order.apply_moves` (Task 2).
  Task 5 is already in `apply.py`; read it first.
- Produces:
  - `have["order"]: list[int]` (live open iids by `relative_position`,
    nulls last, then iid);
  - `have_from_spec(base)["order"]`;
  - `ensure_order(project, spec, base, record, force=False)`.

**Behaviour:**

1. **`spec_from_board`** sorts issues by
   `(rp is None, rp or 0, iid)` instead of by iid.
2. **Live order:**
   - `_live_order(project)` =
     `project.issues.list(state="opened", order_by="relative_position",
     sort="asc", all=True)`, then sorted by the same key.
   - It returns `(iids, {iid: issue})`.
3. **The order records in `diff` and `plan`**, only when `base` is given:
   - `edited = [i["iid"] for i in spec["issues"] if i.get("iid")]`, and
     `old` is the same over `base`.
   - Compute `kind = order.merge(edited, have["order"], old)`.
   - `"skipped"` → `("skipped", "order", "board order changed on GitLab, kept")`.
   - `"drift"` → `("drift", "order", "board order changed on GitLab and in the YAML")`,
     unless `force`, in which case it is treated as `"write"`.
   - `"write"` → one `("changed", "order", f"#{iid} after #{after}")`
     per move, or `f"#{iid} to the top"` when `after` is None.
   - These records go after the issue records and before the notes.
4. **`ensure_order`:**
   - Runs after `ensure_issues`, re-reading the live order.
   - It applies the same merge. On a write, it computes `order.moves`
     against the **fresh** live order and calls `issue.reorder(
     move_after_id=live[after].id)` or `move_before_id=live[before].id`.
   - It records exactly what `diff` would. A rerun after a partial failure
     recomputes from the fresh order, so it continues where it stopped.
5. **The same drift rule as fields:** `apply` refuses drift unless `force`.
   Check how `cli._confirm_writes` treats `drift` records (the `order`
   records use the same kinds, so they are covered).

**Required tests:**

```python
def test_pull_writes_issues_in_board_order(): ...
def test_order_unmanaged_without_base(
    monkeypatch,
): ...  # yaml reordered, no base -> no order records, no reorder()
def test_order_write_uses_minimal_moves(
    monkeypatch,
): ...  # base [1,2,3,4], live same, yaml [4,1,2,3] -> one reorder of 4 before 1
def test_order_skipped_when_only_gitlab_moved(monkeypatch): ...
def test_order_drift_refused_then_forced(monkeypatch): ...
def test_pull_then_plan_is_empty_with_order(monkeypatch): ...
def test_order_rerun_after_partial_failure(
    monkeypatch,
): ...  # reorder raises on 2nd call; rerun finishes
```

The fake `reorder(move_after_id=None, move_before_id=None)` mutates the
fake project's `relative_position` values, so a re-read sees the new order.

- [ ] **Steps:** tests first, then implement, then the full suite and ruff.
  **Close the bead.** Do not commit.

---

### Task 9: `gitboard graph`, digest page, the AI pass, docs

**Files:**
- Modify: `src/gitboard/cli.py`, `tests/test_cli.py`,
  `.claude/commands/board.md`, `CLAUDE.md`, `docs/board-yaml.md`,
  `docs/commands.md`, and `docs/_cli.md` if it is hand-written (check
  `docs/conf.py` and the Makefile `docs` target; if it is generated,
  regenerate it or leave it).

**Interfaces:**
- Consumes: `graph.build`, `graph.subgraph`, `graph.flags`,
  `graph.render_tree`, `graph.render_mermaid`, `graph.cards_from_spec`,
  `graph_html.render_html`, `board.fetch_history`, and the existing `_run`,
  `_need`, `find_spec` and `_history`.

**Behaviour:**

1. **The command:** `gitboard graph [PROJECT] [BOARD] [--from FILE]
   [--milestone M] [--html PATH] [--mermaid]`. Its docstring is "Draw how
   cards block each other on the way to their milestones."
   - **Live:** `board.fetch(path, board)`, then
     `board.fetch_history(project, board, since=now - 30 days)`, and cards
     = history.
   - **Offline:** `apply.load(FILE)` and
     `graph.cards_from_spec(spec, get_config().url)`. Columns come from
     `spec["columns"]`.
   - `g = graph.build(cards)`. With `--milestone`, use `graph.subgraph`;
     an unknown milestone is a `ConfigError` naming the known ones.
   - `flagged` = the keys of the cards named in any `graph.flags` list.
   - `--html PATH` writes `graph_html.render_html(g, title, flagged)` and
     prints `wrote PATH` on stderr.
   - `--mermaid` prints `render_mermaid(g)` with plain `print` (stdout).
   - Otherwise, `out().print(render_tree(g, today))`.
   - A board with no edges and no milestones prints `"no blockers or
     milestones on this board"` on stderr and exits 0.
2. **`digest`:** next to each board's `index.html`, it writes `graph.html`
   from the history it already has (with no extra fetch when the links are
   in the history). `index.html` gains a link to `graph.html`; find where
   it is built in `mail.py`/`cli.py` and add one row.
3. **The plan table order:** in `_changes_table`/`_print_changes`, show
   `note` rows first (as today, if they are first), then `link`/`order`
   rows and `issue` rows whose detail contains `blocked_by`, then the rest.
   Keep it a stable sort. Add a test.
4. **`.claude/commands/board.md`:**
   - Add `graph` to `allowed-tools`, in the same form as the existing
     entries.
   - Add a section, "Blockers, milestones, priority, order". It says:
     - the agent reads `graph --from` and the stats flags;
     - it may stage `milestone`, `milestones:`, `priority::N` labels,
       `blocked_by` additions and removals, and YAML reorders (moving
       entries within `issues:`), but order only takes effect with a
       `.base`;
     - for each flag it proposes one fix;
     - link removals and order moves are listed first in its summary to the
       lead;
     - it never edits a `Blocked by:` footer line in a description.
5. **`CLAUDE.md`:**
   - Add `links.py`, `order.py`, `graph.py` and `graph_html.py` under
     Architecture, one paragraph each in the file's voice.
   - Update "Rendering rules" (the `_urgency` sentence becomes overdue,
     then board order), "apply.py invariants" (link removal allowed; order
     managed only with a base; absent key = unmanaged), the command list
     (`graph`), and "The AI pass writes now" (`graph` allowed).
6. **`docs/board-yaml.md`:** document `milestones:`, `milestone:`,
   `blocked_by:` (three ref forms), `priority::N`, list order = board order
   (with a base), and the footer (read only).
   **`docs/commands.md`:** the `graph` command.

**Required tests** (`tests/test_cli.py`, using the existing CliRunner
patterns and offline `--from`):

```python
def test_graph_from_file_prints_tree(tmp_path): ...
def test_graph_mermaid_to_stdout(tmp_path): ...
def test_graph_html_writes_file(tmp_path): ...
def test_graph_unknown_milestone_errors(tmp_path): ...
def test_graph_empty_board_says_so(tmp_path): ...
def test_changes_table_puts_link_and_order_rows_first(): ...
```

- [ ] **Steps:** tests first, then implement, then the full suite and ruff.
  Then run it by hand:
  `env -u FORCE_COLOR PYTHONPATH=src .venv/bin/python -m gitboard.cli graph --from <tmp yaml with 5 cards, 2 milestones>`,
  plus `--mermaid` and `--html`. Paste the terminal output into the bead's
  close reason (the first 15 lines). **Close the bead.** Do not commit.

---

## Final review (orchestrator)

After wave 4: one reviewer agent reads the whole branch diff against the
spec and this plan's Review Focus. Then:
- run the full suite and ruff;
- run `gitboard graph --from boards/demo.yaml` after adding blockers and
  milestones to a copy of `demo.yaml` in `$CLAUDE_JOB_DIR/tmp`.

File any follow-ups as beads with `discovered-from:gb-283`. These are
already known:
- an estimate-weighted critical path;
- a TUI graph key;
- the live check on work Premium (under gb-mko).
