# The board YAML

One file per board in `boards/`. `pull` writes it, you edit it, `plan`
diffs it, `push` writes it back.

## Annotated example

```yaml
project: team/backend
board: Dev Board
# create_project: true         # make the project if it does not exist

people:                        # display name -> GitLab username; used by ingest
  Alice Ng: alice
  Bob Ruiz: bruiz

columns:                       # board lists, in order. Each is a label.
  - name: Doing
    color: gitlab blue         # a name from the table below, or "#428bca"
  - name: Blocked
    color: crimson
  - name: Review
    color: "#5cb85c"

labels:                        # non-column labels: colour + description, so
  - name: type::bug            # scoped labels stop getting random colours
    color: crimson
    description: A defect in shipped behaviour
  - name: stale
    color: gray

milestones:                    # optional; push creates and updates, never closes
  - title: Beta launch
    due_date: "2026-11-01"
    description: What "beta" means for us.

issues:                        # list order = GitLab board order (with a .base)
  - title: Set up the board from YAML     # identity when there is no iid
    iid: 12                    # from pull; the match key, so a new title here is a rename
    labels: [Doing, stale, "priority::1"]   # column labels place it; priority::1 is most urgent
    milestone: Beta launch     # must be under milestones:; null clears it
    blocked_by: [9, "infra/platform#4", "Rotate the PAT"]   # iid, other project, or a title here
    assignee: alice
    due_date: "2026-09-01"     # quote it, or YAML makes a date object
    description: |
      Markdown. GitLab strips the trailing newline; norm_text hides that.
      Verify steps are a task list, so the card shows a progress bar:
      - [ ] Open /login, sign in as a viewer
      - [ ] Expect the dashboard
    notes:                     # comments to post on push; already-posted bodies are skipped
      - "Moved to Doing after the review on Monday."
      - "Q: is the token rotation still blocking this?"   # a question for the team
    discussion:                # pulled with --notes; read-only, push ignores it
      - by: bruiz
        at: "2026-08-30"
        body: "Blocked on the token rotation."

  - title: Decide whether to add the MCP server
    labels: [Blocked, Review]  # two columns: shows in both, counted once

  - title: Rotate the PAT      # no labels -> Backlog (never declared)
```

## Keys

`project`, `board`
: Required. `create_project: true` makes a missing project.

`people`
: Map of display name to GitLab username. `ingest` uses it to turn
  "Alice Ng" in a tasks file into `assignee: alice`. Not sent to GitLab.

`emails`
: Map of GitLab username to email address. `digest` writes a `.eml` for
  every assignee listed here (everyone still gets a `.md`). Not sent to
  GitLab. Example: `emails: {alice: alice@example.com}`.

`estimates`
: Optional map. `suggest_due` (default `true`; `false` makes
  `gitboard estimate` print its table and write nothing), `method` (`p85`
  default, or `median`), `min_samples` (default 5: the fewest finished cards
  a bucket needs). An estimate is that percentile of the person's finished
  cards of the same `type::`, widening to the person, the team's `type::`,
  then the team until a bucket is big enough; none is big enough, no
  estimate. `stats` and the digests list **Tight dates** — due dates earlier
  than that — whatever `suggest_due` says — and **Late milestones**: the
  estimates along a milestone's longest blocker chain, summed, run past its
  due date. Not sent to GitLab.

Scoped labels
: `epic::<name>`, `story::<name>`, `type::<bug|task|chore|verify>` are
  plain labels (CE has no epics object) and stay in `labels:`. `stats`
  groups by them; `ingest` tags new cards `type::verify`; the `/board`
  agent proposes them for unlabelled cards. One of each per card.

`columns[].name`, `columns[].color`
: A list is a label. Colour by name (below) or hex.

`labels[].name`, `labels[].color`, `labels[].description`
: Labels that are not columns (`type::bug`, `epic::Billing`, `stale`).
  `push` creates a missing one (default colour gitlab blue) and fixes a
  colour or description that differs; `plan` shows both as `label` rows.
  `pull` writes every non-column label a card on the board carries, so the
  look survives a move to another project. A name that is also a column is
  an error: its colour lives under `columns:`.

`issues[].title`
: The identity when the entry has no `iid`: renaming such an entry creates
  a second issue and the old one stays open. Stripped of surrounding
  whitespace on both sides before comparing.

`issues[].iid`
: Written by `pull`. When present it is the match key, so changing the
  title of an entry that has one **renames** that issue. Never invent one;
  leave it off issues you add.

`issues[].labels`, `assignee`, `due_date`, `description`
: Optional. Labels that are column names place the issue; others are just
  labels, and `push` only ever adds: a label the team put on in the web UI
  survives a push that does not list it. Two labels have a meaning to the
  flow: `stale` (sat in a column past the threshold, see `report`) and
  `re-verify` (changed after someone verified it). A description whose
  verify steps are a `- [ ]` task list gets GitLab's task progress on the
  card, and `ingest` writes them that way.

`milestones[].title`, `due_date`, `description`
: Optional. `push` creates a missing milestone and fixes a due date or
  description that differs; it never closes or deletes one. A group
  milestone with the same title counts as existing. `pull` writes an entry
  for every milestone a card carries and every active (not closed) project
  milestone.

`issues[].milestone`
: A title from `milestones:` (anything else is a load error). **No key
  leaves the card's milestone alone**; `milestone: null` clears it.

`issues[].blocked_by`
: The cards this one waits on, as a list of any of:
  - an int, an iid in this project (`9`);
  - a string `group/project#iid`, a card in another project;
  - the exact title of another card in this file, which `push` resolves
    after creating that card, so new cards can block each other.

  **No key leaves the card's links alone**; `blocked_by: []` removes them.
  A card blocking itself, a title that names no card, or a cycle among the
  file's cards is a load error. Written as native GitLab `is_blocked_by`
  links, which need Premium; on an instance that downgrades them to
  `relates_to`, `push` removes the stray link and stops with one error.

Description footer (read only)
: A description whose last line is `Blocked by: #9, infra/platform#4`
  counts as blockers too, so a Free/CE instance can still draw a graph.
  `pull` reads the union of links and footer; gitboard never writes a
  footer. Dropping a ref only the footer holds from `blocked_by` is not a
  change: `plan` and `push` each report one `skipped` row and the ref
  stays. Edit the description in GitLab.

`priority::N`
: An ordinary label, `priority::1` (most urgent) to `priority::4`. One per
  card; two is a load error. A card without one ranks below 4. `stats`
  and `graph` flag a blocker that ranks below the card it blocks.

List order
: The order of `issues:` is GitLab's board order (one manual order per
  project; every list shows it). `pull` writes it; moving entries
  reorders the board, with the fewest drags. **Only with a `.base`**: a
  hand-written file never reshuffles the board. When the team dragged
  cards since the pull and you did not, `plan` shows `skipped`; when both
  did, `drift`. Cards new on either side go wherever GitLab puts them.

`issues[].notes`
: List of strings. Each is posted as a comment on `push`, with a
  `*staged via gitboard*` first line so the team can tell a staged note
  from a typed one; a body that is already on the issue is skipped, so
  re-pushes do not duplicate. This is how an offline agent replies to team
  feedback. A note starting `Q:` is a question for the team; the answer
  comes back as `discussion:` on the next `pull --notes`.

`issues[].discussion`
: List of `{by, on, body}`, written by `pull --notes`. Read-only context for
  the AI pass. `push` ignores it.

## Change kinds

`plan` reads three things when `<spec>.base` exists: the YAML (what you
want), the live board (what is), and the base (what you pulled). Each row
in its table is one of:

| Kind | Meaning | `push` |
|---|---|---|
| `added` | in the YAML, not on the board (or a note not yet posted) | creates / posts |
| `changed` | YAML differs from both board and base; shown as `old -> new` | writes the YAML value |
| `drift` | board differs from base, YAML does not: the team moved it since the pull | keeps the board's value; refuses unless `--ignore-drift` |
| `skipped` | the issue is closed on GitLab | nothing; never recreated |

Without a base, every difference is `changed` and there is no drift to
detect: that is the plain two-way plan.

## Invariants

- **Identity is `iid`, else title.** A retitle with an `iid` is a rename; a
  retitle without one is a second issue. Duplicate titles in one file are
  an error.
- **Additive.** `push` never deletes or closes an issue or a milestone.
  Removing an issue from the file leaves it on the board; removing a label
  leaves it on the issue. A blocker link is the one thing it removes, and
  only when the card has a `blocked_by` key that no longer lists it.
- **An absent key is unmanaged.** No `blocked_by`, no `milestone`, no
  `.base` for the order: `push` leaves that part of the board alone.
  Closing is an explicit act (`migrate-comments --close-source`). Closed
  issues are skipped, not reopened or recreated.
- **Verify is one-way for the agent.** The AI pass may move an issue into
  `Verify`; `Done` and `Failed` come from a person's `verified:` /
  `failed:` comment, read by `ingest`.
- **Idempotent.** `pull` then `plan` is empty; `push` twice writes nothing
  the second time. Three normalisations keep it so, and each is pinned by a
  test:
  - Colours are normalised to hex on load (the API only speaks hex); `pull`
    maps known hexes back to names.
  - `due_date: 2026-09-01` unquoted is a `datetime.date` to PyYAML, which
    cannot be JSON-encoded; it is coerced to ISO. Quoting it avoids the
    question.
  - GitLab strips a description's trailing newline; `|` keeps it. Both
    sides are trimmed before comparing.

## Colours

Case, hyphens and underscores are ignored (`Rose-Red` is `rose red`).

| | | | |
|---|---|---|---|
| red | crimson | rose red | magenta pink |
| pink | dark coral | orange | carrot orange |
| aztec gold | champagne | yellow | titanium yellow |
| green | green cyan | green screen | dark green |
| dark sea green | medium sea green | teal | blue |
| gitlab blue | blue gray | lavender | purple |
| dark violet | deep violet | brown | gray |
| charcoal | black | white | |
