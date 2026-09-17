# The board YAML

One file per board in `boards/`. `pull` writes it, you edit it, `plan`
diffs it, `apply` writes it back.

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

issues:
  - title: Set up the board from YAML     # identity when there is no iid
    iid: 12                    # from pull; the match key, so a new title here is a rename
    labels: [Doing, stale]     # column labels place it; others (stale, re-verify) are follow-ups
    assignee: alice
    due_date: "2026-09-01"     # quote it, or YAML makes a date object
    description: |
      Markdown. GitLab strips the trailing newline; norm_text hides that.
      Verify steps are a task list, so the card shows a progress bar:
      - [ ] Open /login, sign in as a viewer
      - [ ] Expect the dashboard
    notes:                     # comments to post on apply; already-posted bodies are skipped
      - "Moved to Doing after the review on Monday."
      - "Q: is the token rotation still blocking this?"   # a question for the team
    discussion:                # pulled with --notes; read-only, apply ignores it
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

`columns[].name`, `columns[].color`
: A list is a label. Colour by name (below) or hex.

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
  labels, and `apply` only ever adds: a label the team put on in the web UI
  survives an apply that does not list it. Two labels have a meaning to the
  flow: `stale` (sat in a column past the threshold, see `report`) and
  `re-verify` (changed after someone verified it). A description whose
  verify steps are a `- [ ]` task list gets GitLab's task progress on the
  card, and `ingest` writes them that way.

`issues[].notes`
: List of strings. Each is posted as a comment on `apply`, with a
  `*staged via gitboard*` first line so the team can tell a staged note
  from a typed one; a body that is already on the issue is skipped, so
  re-applies do not duplicate. This is how an offline agent replies to team
  feedback. A note starting `Q:` is a question for the team; the answer
  comes back as `discussion:` on the next `pull --notes`.

`issues[].discussion`
: List of `{by, on, body}`, written by `pull --notes`. Read-only context for
  the AI pass. `apply` ignores it.

## Change kinds

`plan` reads three things when `<spec>.base` exists: the YAML (what you
want), the live board (what is), and the base (what you pulled). Each row
in its table is one of:

| Kind | Meaning | `apply` |
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
- **Additive.** `apply` never deletes or closes. Removing an issue from the
  file leaves it on the board; removing a label leaves it on the issue.
  Closing is an explicit act (`migrate-comments --close-source`). Closed
  issues are skipped, not reopened or recreated.
- **Verify is one-way for the agent.** The AI pass may move an issue into
  `Verify`; `Done` and `Failed` come from a person's `verified:` /
  `failed:` comment, read by `ingest`.
- **Idempotent.** `pull` then `plan` is empty; `apply` twice writes nothing
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
