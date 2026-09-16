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
  - title: Set up the board from YAML     # identity. Never retitle.
    iid: 12                    # from pull; informational, apply ignores it
    labels: [Doing]
    assignee: alice
    due_date: "2026-09-01"     # quote it, or YAML makes a date object
    description: |
      Markdown. GitLab strips the trailing newline; norm_text hides that.
    notes:                     # comments to post on apply; already-posted bodies are skipped
      - "Moved to Doing after the review on Monday."
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
: The identity. Renaming creates a second issue; the old one stays open.

`issues[].iid`
: Written by `pull`. Informational. Never invent one; leave it off issues
  you add.

`issues[].labels`, `assignee`, `due_date`, `description`
: Optional. Labels that are column names place the issue; others are just
  labels.

`issues[].notes`
: List of strings. Each is posted as a comment on `apply`; a body that is
  already on the issue is skipped, so re-applies do not duplicate. This is
  how an offline agent replies to team feedback.

`issues[].discussion`
: List of `{by, on, body}`, written by `pull --notes`. Read-only context for
  the AI pass. `apply` ignores it.

## Invariants

- **Title is identity.** No rename. Duplicate titles in one file are an error.
- **Additive.** `apply` never deletes or closes. Removing an issue from the
  file leaves it on the board. Closing is an explicit act
  (`migrate-comments --close-source`).
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
