# Blockers, priority and the milestone graph — design

Bead: gb-283 (epic). Approved in conversation 2026-09-27 (revised the same
day: no GitLab Duo; the team works cards in GitLab directly; perch's vision
folded in).

## Why

Today `Blocked` is a column. It says a card is stuck, never on what. Which
card waits on which, which card matters most to the next milestone, and what
order the team should work in all live in the lead's head. The team works in
parallel, and in chains that intertwine, towards the same dates.

**Split of work.** The team works cards directly in GitLab: they click
"blocked by", set milestones and drag cards. The lead reorders and
reprioritises with Claude (`/board`), which stages YAML edits the lead
approves. There is no GitLab Duo. GitLab stores the edges, but it has no
view of the whole dependency network (its epics for one have been open for
years). gitboard draws that picture, and flags where the board contradicts
itself.

**The larger system.** gitboard owns the work, Budgie owns the money, and
perch (`../perch`) joins the two. perch never calls GitLab, and a number it
needs goes into the tool that owns it. perch's planned weekly block reports
"what is waiting on someone else, and for how long". Its quarterly report
covers "what waiting costs", and its v2 is "what no longer fits after a
cut". All three need blocker edges, block start times, milestones and
priority from gitboard's `stats --dump`. Dollars stay in perch: gitboard
computes no cost.

## What

- **A.** The dump carries `blocked_by` (with state and start time) and
  `priority` for each card.
- **B.** The lead, with Claude, can change a card's milestone, due date,
  `priority::` label, blocker links and manual board order. Every change
  goes through the usual YAML → `plan` → go-ahead → `apply` flow.
- **C.** `show` sorts overdue cards first, then by GitLab's board order.
- **D.** Four kinds of flag help reprioritise: blocked-column mismatch,
  priority inversion, date inversion and unowned blocker. They are never
  moves.
- **E.** `gitboard graph`: a terminal tree, `--html` and `--mermaid`.

Not in v1 (these become follow-up beads): a critical path weighted by
`estimate.py` days, a TUI key for the graph, and writing blockers as a
description footer.

## A. Data: the history dict and the dump

`board.fetch_history` adds these fields to each card. perch's loader ignores
unknown keys (`perch/core/board.py`), so the change is additive.

```json
"blocked_by": [{"ref": "9", "state": "opened", "since": "2026-09-20T10:02:11Z"}],
"priority": 1
```

- `ref` is `"9"` for a card in this project and `"group/proj#4"` for a card
  in another project.
- `state` is `opened` or `closed`. A blocker sitting in `Done` also counts as
  closed; this is `stats.done_at`'s rule.
- `since` is the link's `link_created_at`. For a footer ref it is null.
- The wait time is (the blocker's done time, or now) minus `since`. perch
  computes it, or `stats` does for the digest.
- `priority` is the digit of the card's lowest `priority::N` label (1 is
  most urgent), or null when there is none. `milestone` is already present.

## B. What the lead and Claude can change

### YAML shape

```yaml
milestones:                 # optional
  - title: Beta launch
    due_date: 2026-11-01
    description: What "beta" means for us.

issues:                     # list order = GitLab board order (see Order)
  - title: Wire up board reader
    iid: 12
    labels: [Doing, "priority::1"]
    milestone: Beta launch
    blocked_by: [9, "infra/platform#4", "Token rotation policy"]
```

- A `blocked_by` entry is one of three things:
  - an int, meaning an iid in this project;
  - a string `group/project#iid`, meaning a card in another project;
  - the exact stripped title of another card in this spec. `apply`
    resolves a title to the iid after it has created that card, so an agent
    can link cards that do not exist on GitLab yet.
- `load()` raises `SpecError` when:
  - a title ref names no card;
  - a card blocks itself;
  - the spec's cards form a cycle (checked with stdlib
    `graphlib.TopologicalSorter`);
  - a `milestone:` is not in `milestones:`. `pull` writes an entry for
    every milestone a card carries, so a pulled YAML always passes this
    check.
- A card with **no `blocked_by` key** is unmanaged: `apply` leaves its
  links alone. `blocked_by: []` removes them. Without this, a hand-written
  spec would wipe every link on the board. `milestone` follows the same
  rule: an absent key is unmanaged, and `null` clears the milestone.
- `wanted_issue` normalises refs: a same-project `group/project#9` becomes
  `9`, and the list is sorted with duplicates dropped.
- `priority::N` is an ordinary label, so the existing label path already
  writes it. `load()` rejects a card carrying two `priority::` labels.

### Merge

`milestone` and `blocked_by` join `issue_changes`, so plan and apply keep
one decision point. The existing `skipped` rule applies (GitLab changed the
field and the YAML did not), and so does `drift` (both changed it). When the
key is absent, the field is left out of the comparison.

### Milestones

`ensure_milestones(project, spec, record)` is additive: it creates missing
milestones and updates their `due_date` and `description`, and never closes
or deletes one. A card's milestone title resolves to an id before any write
(the way `resolve_users` works), never inside the diff. A group milestone
with the same title counts as existing.

### Links

- The write path uses native GitLab links only. The work instance is
  Premium.
  - Read: `issue.links.list()`, keeping `link_type == "is_blocked_by"`.
  - Add: `issue.links.create({"target_project_id", "target_issue_iid",
    "link_type": "is_blocked_by"})`.
  - Remove: `issue.links.delete(issue_link_id)`.

  Removing a link is allowed, like removing a label: a link is reversible
  metadata. `apply` still never deletes or closes an issue or a milestone.
- **Reads also parse a footer.** A description whose last line is
  `Blocked by: #9, infra/platform#4` counts as blocker refs. This keeps the
  local Free/CE demo working, because CE downgrades `blocks` to
  `relates_to`. `pull` takes the union of native links and the footer, and
  leaves the description text as it is. gitboard never writes a footer.
  A footer ref already counts as present, so it is never duplicated as a
  native link. Removing a ref that only the footer holds is reported as
  `skipped` ("footer ref, edit the description").
- **Cost:** one links request per open issue. Only `pull`, `graph`, `stats`
  and `digest` pay it. A live `show` does not.

### Order

GitLab keeps one manual order per project (`relative_position`), and every
board list shows cards in that order. The YAML's `issues:` list order
represents it.

- `pull` writes issues sorted by `relative_position` (ascending, nulls
  last, then iid). The source is
  `issues.list(order_by="relative_position", sort="asc")`; confirm that
  ordering in the live check.
- **Order is managed only when a `.base` exists**, meaning the file was
  pulled. A hand-written YAML never reshuffles the board.
- The order is one field in the three-way merge:
  - The YAML's order equals the base's: nothing to do. If GitLab's order
    changed meanwhile, `plan` reports `skipped` ("order changed on GitLab,
    kept").
  - The YAML's order differs, and GitLab's still equals the base's: write.
  - Both changed: `drift` (`!`). `apply` refuses unless `--force`.
- The comparison is only over cards present in both the YAML and GitLab.
  Cards created in this apply go wherever GitLab puts them. The next pull
  shows their real place.
- **Minimal moves:** keep the longest run of cards that are already in the
  wanted order (a longest increasing subsequence, stdlib, O(n log n)). Move
  each other card with `issue.reorder(move_before_id=<previous card's
  global id>)`. GitLab's `move_before_id` names the card that ends up
  *before* this one; the names read backwards, and the build caught it.
  A card moving to the front uses `move_after_id=<first kept card>`.
  `plan` shows each move as `~ order: #12 after #9`.

### The AI pass

`.claude/commands/board.md`:

- gains `graph` in `allowed-tools`;
- may stage `milestone`, `milestones:`, `priority::` labels, `blocked_by`
  additions and removals, and YAML reorders;
- works from the flags in D. For each flag it proposes the fix (raise the
  blocker's priority, move it up, pull its date in, assign it), and the lead
  approves the plan.

`plan` lists link removals and order moves right after notes, so the lead
sees them before approving. The rule "the agent may not remove links" is not
needed: every plan gets a person's go-ahead.

## C. `show` sort order

`board_columns` currently sorts by `_urgency`: overdue, then soonest due,
then newest. The new key is **overdue first, then GitLab's board order**
(`relative_position`, nulls last, then newest). When the lead and Claude
reorder the board, `show`, the TUI and the digest show the order the team
sees in GitLab, and overdue cards still surface in a truncated column.
`as_markdown` keeps its line shape; only the line order changes. Update the
"Rendering rules" in CLAUDE.md and the `/board` command to match.
`columns_from_spec` uses the YAML's list order.

## D. Flags

These go into `stats.summarise` under `flow`. They appear in the stats
markdown, the digest and the `/board` report, and their counts go into
`stats.jsonl`. They are flags, never moves (the weak-verdict rule).

"Open" means not closed and not in Done. A card's priority is `priority`,
where null ranks below 4. A card's date is its `due_date`, else its
milestone's `due_date`.

| Flag | Condition |
|---|---|
| `blocked_stale` | The card is in the Blocked column, and none of its blockers is open. |
| `blocked_unmarked` | The card has an open blocker, and it is not in Blocked, Verify, Done or Failed. |
| `priority_inversion` | An open blocker's priority ranks below the card's. |
| `date_inversion` | An open blocker's date is later than the card's date. |
| `unowned_blocker` | An open, unassigned card blocks an open card that has a milestone. |

The two `blocked_*` flags apply only when the board has a column named
Blocked (case-insensitive). Every flag names both cards, for example
"#12 (P1) waits on #9 (P3)". `for_person` gives each person the flags on
their own cards, and `three_moves` may pick an `unowned_blocker` or a
`priority_inversion` as a move.

## E. Views: `gitboard graph [PROJECT] [--from FILE] [--milestone M]`

The pure core is a new module, `graph.py` (stdlib only). It takes card dicts
(live, or `columns_from_spec` stand-ins) and returns:

- the nodes (cards and milestones) and the edges (blocker → card, card →
  milestone);
- `downstream[iid]`: how many cards transitively wait on each card;
- `critical[milestone]`: the longest chain into each milestone, counted in
  hops, with ties broken by the lowest iid;
- `layers`: each node's longest-path depth, with milestones in the last
  layer and order inside a layer by barycenter over two sweeps. This is a
  deliberate small version of the Sugiyama layout.

Renderers:

- **Terminal (default)**: one rich tree per milestone. The root shows the
  title, due date, days left and % done. Its children are the milestone's
  cards that no other card in the milestone waits on, and under each card
  are its blockers, recursively. A card already printed shows as
  `(see #9 above)`. Each line is `issue_line` plus the priority,
  `⇠ N waiting`, a `★` when the card is on the critical chain, and the
  marks for any D flags. Cards that have edges but no milestone go under a
  final `No milestone` root. The tree goes to stdout through `out()`.
- **`--html PATH`**: one self-contained file. Inline SVG drawn from
  `layers`, with blockers on the left and milestones as large nodes on the
  right. Closed blockers are faded and flagged edges are drawn in red. About
  50 lines of inline JS:
  - clicking a node highlights everything upstream and downstream of it;
  - hovering shows the title, assignee, column, priority and due date;
  - clicking empty space clears the highlight.

  There is no CDN and no vendored library, so the page works air-gapped.
  Colours come from `mail.COLUMN_COLORS`. `digest` also writes `graph.html`
  next to `index.html` and links to it.
- **`--mermaid`**: prints `flowchart LR` to stdout. Node ids are `i12` and
  `m_<slug>`, labels are quoted and escaped, milestones are stadium
  shapes, and closed cards get a `done` class. To show it in GitLab, a
  person pastes it into a description or wiki page; GitLab renders Mermaid.

## Errors

- Some instances have no blocking links: they return 403 or 404, or, like
  CE, they silently create `relates_to` instead.
  - On a write, `apply` checks the returned `link_type`. A downgraded link
    is deleted again, and `apply` raises a `GitlabProblem` once: "blocking
    links need GitLab Premium".
  - On a read, a 403 or 404 falls back to footer refs only, with that
    warning printed once.
- A `blocked_by` ref that does not exist, or that the token cannot read, is
  reported as `skipped` ("#12: blocker infra/x#4 not found"). `apply` skips
  that one link and applies everything else.
- A reorder that fails partway: GitLab reorders one card per call, so the
  cards already moved stay moved. The error names the card, and a rerun
  continues from there, because each move checks the live order first.

## Tests

The fake API grows `links` (list/create/delete, with `link_created_at`),
`milestones` (list/create/save), `reorder`, and `relative_position` on
issues.

- `load`: title refs, an unknown title, a self-block, a cycle, a missing
  milestone, two `priority::` labels.
- Footer parse; the union of native links and the footer is one edge per
  card. `pull` then `plan` is empty, order included.
- `issue_changes` on `blocked_by` and `milestone`: added, removed, skipped,
  drift. An absent key is unmanaged.
- Order:
  - the fewest moves for a known permutation;
  - no base means no reorder;
  - skipped when only GitLab changed;
  - drift when both changed, and `--force` writes;
  - a rerun after a partial failure.
- `fetch_history` carries `blocked_by`, with state and since, and
  `priority`.
- Each D flag, and no `blocked_*` flags without a Blocked column.
- `graph.py`: downstream counts, the critical chain with its tie-break, the
  layers, and a shared blocker printed once in the tree.
- Mermaid escaping. HTML parses with `html.parser`, has one `<svg>`, and has
  no external `src=` or `href="http`.
- `show` order: overdue first, then `relative_position`.

The live check (under Track A, gb-mko) covers:

- native links, reorder and `order_by=relative_position` on the work
  Premium instance;
- the footer read on local CE;
- the Mermaid output pasted into a GitLab description;
- a perch run on a dump that has the new fields.
