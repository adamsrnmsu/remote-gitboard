# Blockers and the milestone graph — design

Bead: gb-283 (epic). Approved in conversation 2026-09-27.

## Why

Today `Blocked` is a column: it says a card is stuck, never on what. Which
card waits on which, and why a card matters to the next milestone, lives in
the lead's head. The team works in parallel and in chains that intertwine,
and nobody can see that they converge on the same date.

The board should carry the edges itself. Then gitboard can draw the chains
converging on a GitLab milestone, show each person why their card matters
("3 cards and Beta launch wait on this"), and flag the Blocked column when
it disagrees with the links. The lead no longer has to curate that picture by
hand.

## What

- Cards declare `blocked_by:` and `milestone:` in the board YAML. `pull`
  reads them and `plan`/`apply` write them, through the existing three-way
  merge.
- A new `milestones:` section, applied as native GitLab milestones.
- `gitboard graph`: a terminal tree (default), `--html PATH` (a
  self-contained interactive page) and `--mermaid` / `--stage SPEC` (a
  Mermaid flowchart, staged into each milestone's description for GitLab to
  render).
- Two flags in `stats` and `digest` where the Blocked column disagrees with
  the links.

Not in v1 (follow-up beads): a critical path weighted by `estimate.py` days
("does this chain make the milestone late?"), and a TUI key for the graph.

## 1. YAML shape

```yaml
milestones:                 # optional
  - title: Beta launch
    due_date: 2026-11-01
    description: What "beta" means for us.

issues:
  - title: Wire up board reader
    iid: 12
    milestone: Beta launch
    blocked_by: [9, "infra/platform#4", "Token rotation policy"]
```

- A `blocked_by` entry is one of: an int (an iid in this project), a string
  `group/project#iid` (a card in another project), or the exact stripped
  title of another card in this spec. Titles let an agent link cards that
  have no iid yet. `apply` resolves a title to the card's iid after it has
  created the card.
- `load()` raises `SpecError` on:
  - a `blocked_by` title that names no card in the spec;
  - a card that blocks itself;
  - a cycle among the spec's cards, found with stdlib
    `graphlib.TopologicalSorter` (no networkx);
  - a `milestone:` that is not in `milestones:` (`pull` writes an entry
    for every milestone a card carries, so a pulled YAML always passes).
- A card with **no `blocked_by` key** is unmanaged: `apply` never touches
  its links. `blocked_by: []` means "no blockers" and removes links.
  Without this rule, a hand-written spec would wipe every link on the board.
  `milestone` works the same way: an absent key is unmanaged, and `null`
  clears it.
- Refs are normalised in `wanted_issue`: same-project `group/project#9`
  becomes `9`, the list is sorted, and duplicates are dropped. That way the
  YAML and GitLab compare cleanly.

## 2. Read and write

### Merge

`milestone` and `blocked_by` join `labels`, `description`, `due_date` and
`assignee` in `issue_changes`. Plan and apply keep sharing one decision
point, so `skipped` (GitLab changed it and the YAML did not) and `drift`
(both changed it) work for the new fields with no new code path. When the
spec entry lacks the key, the field is left out of the comparison.

### Milestones

`ensure_milestones(project, spec, record)` is additive. It creates missing
milestones and updates `due_date` and `description`, and it never closes or
deletes a milestone. A card's milestone title resolves to an id the way
`resolve_users` resolves usernames, before any write and never inside the
diff. A group milestone with that title counts as existing (GitLab lets a
project's issues use its group's milestones). `pull` writes a `milestones:`
entry for every milestone a card carries.

### Links: two modes, one field

A new config key, `links`, set in `gitboard.toml`, `GITBOARD_LINKS` or
`--links`:

- **`native`** (the default; the work instance is Premium). Read with
  `issue.links.list()`, keeping `link_type == "is_blocked_by"`. Add with
  `issue.links.create({"target_project_id", "target_issue_iid",
  "link_type": "is_blocked_by"})`. Remove with
  `issue.links.delete(issue_link_id)`.
- **`footer`** (dev on Free/CE, where GitLab downgrades `blocks` to
  `relates_to` and drops the direction). The description's last line is
  `Blocked by: #9, infra/platform#4`. GitLab autolinks those references, so
  they are clickable in its UI.

**Read in either mode**: `pull` takes the union of the native
`is_blocked_by` links and the footer. It strips the footer from
`description`, so `description` and `blocked_by` never show up as one
change. When the team clicks "blocked by" in GitLab's UI, gitboard sees it.

**Write**: in `native` mode, links are added and removed. In `footer` mode,
the description is rebuilt as body + footer whenever `description` or
`blocked_by` is written. `norm_text` applies to the body only.

**Removal is allowed**, like removing a label. A link is reversible
metadata, not data. `apply` still never deletes or closes an issue or a
milestone.

**Cost**: one links request per open issue. Only `pull`, `graph`, `stats`
and `digest` pay it (`fetch_history` already pays one request per issue for
label events). A live `show` does not; `show --from` gets links from the
YAML for free.

### Offline

`have_from_spec` carries `milestone` and `blocked_by` from the `.base`, so
`plan FILE --against BASE` covers them. `columns_from_spec` puts them on the
stand-in issues, so `graph --from FILE` works in the container.

## 3. Views: `gitboard graph [PROJECT] [--from FILE] [--milestone M]`

A pure core, `graph.py`, stdlib only. It takes the card dicts (live or from
a spec) and returns:

- `nodes`: cards and milestones, each with its column, assignee, due date,
  and open/closed state. "Open" means not closed and not in `Done`.
- `edges`: blocker → card, and card → its milestone.
- `downstream[iid]`: how many cards, transitively, wait on this card.
- `critical[milestone]`: the longest chain into that milestone, in hops.
  Ties break by lowest iid, so the output is stable.
- `layers`: the layer of each node. It is the longest-path depth from the
  sources, and every milestone goes in the last layer. Order within a layer
  is by barycenter, two sweeps. This is a deliberate small version of the
  Sugiyama layout, in `graph.py`.

Renderers:

- **Terminal (default)**: one rich tree per milestone. The root shows the
  title, due date, days left and % of its cards done. Its children are the
  milestone's cards that no other card in the milestone waits on. Under each
  card are its blockers, recursively. A node printed once prints as
  `(see #9 above)` after that. Each card line uses `issue_line` plus
  `⇠ N waiting`, and a `★` marks cards on the critical chain. Cards with no
  milestone but with edges go under a final `No milestone` root. Output goes
  to stdout through `out()`, like `show`.
- **`--html PATH`**: one self-contained file. It is inline SVG drawn from
  `layers`, with blockers on the left and milestones as large nodes on the
  right. Closed blockers are faded. About 50 lines of inline JS:
  - clicking a node highlights everything upstream and downstream of it,
    which is its path to the milestone;
  - hovering shows the title, assignee, column and due date;
  - clicking empty space clears the highlight.

  There is no CDN and no vendored library, so the page works air-gapped.
  The colours come from `mail.COLUMN_COLORS`, so the page matches the
  digest. `digest` also writes `graph.html` next to `index.html` and links
  to it.
- **`--mermaid`**: prints `flowchart LR` to stdout. Node ids are `i12`,
  `m_<slug>`, and labels are quoted and escaped. Milestones are stadium
  shapes, and closed cards get a `done` class.
- **`--stage SPEC`**: writes each milestone's Mermaid subgraph into that
  milestone's `description`, between `<!-- gitboard:graph -->` and
  `<!-- /gitboard:graph -->`. Text outside the markers is kept. It writes
  the file only when something changed. Then the normal plan → go-ahead →
  `apply` puts it into GitLab, which renders Mermaid. `apply` stays the only
  writer.

## 4. Truthfulness flags

Only when the board has a column named `Blocked` (case-insensitive),
`stats.summarise` adds:

- `flow.blocked_stale`: a card sits in Blocked, but none of its blockers is
  open.
- `flow.blocked_unmarked`: a card has an open blocker, but it is not in
  Blocked and not in Verify/Done/Failed.

Both are flags in the stats markdown, the digest and `stats.jsonl` counts.
They are never moves, the same rule as weak verdicts. `fetch_history` adds
`blocked_by` (resolved refs plus each blocker's open/closed state) and
`milestone` to each history dict.

## The AI pass

`.claude/commands/board.md` gains `graph` in allowed-tools and a rule. The
agent may add `blocked_by` entries and set `milestone`, and it may stage the
Mermaid block with `graph --stage`. It must not remove a `blocked_by` entry
it did not add in this session; removing one is a person's call, and the
agent suggests it instead. The plan table shows link changes as
`~ #12: blocked_by [9] -> [9, 14]`.

## Errors

- A 403 or 404 on a links request in `native` mode becomes a
  `GitlabProblem`: "issue links not available; set `links = "footer"` in
  gitboard.toml". A Free instance fails once, with that message, not once
  per card.
- `blocked_by` pointing at an issue that does not exist, or that the token
  cannot read: `plan` and `apply` report it as `skipped` ("#12: blocker
  infra/x#4 not found") and skip that one link. The rest applies.

## Tests

The fake API grows `links` (list/create/delete, `is_blocked_by` type) and
`milestones` (list/create/save).

- `load`: title refs, an unknown title, a self-block, a cycle, a missing
  milestone.
- Footer parse and render round trip. `pull` then `plan` is empty in both
  modes. A native link and a footer ref to the same card are one edge.
- `issue_changes` on `blocked_by` and `milestone`: added, removed, skipped,
  drift. An absent key is unmanaged.
- `graph.py`: downstream counts, the critical chain with its tie-break,
  layers (milestones in the last layer), a shared blocker printed once in
  the tree.
- Mermaid escaping. `--stage` keeps text outside the markers and is
  idempotent.
- `blocked_stale` / `blocked_unmarked` flags, and no flags without a
  Blocked column.
- HTML: parses (stdlib `html.parser`), has one `<svg>` and no external
  `src=`/`href=http`.

Live check (Track A): native links on the work Premium instance, footer mode
on local CE, and Mermaid rendering in a milestone description.
