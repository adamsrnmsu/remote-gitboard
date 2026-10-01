# Commands

`gitboard` below means `PYTHONPATH=src .venv/bin/python -m gitboard.cli`
or the `make link` wrapper. Full option reference: {doc}`reference`.

## Cheat sheet

```bash
# read (read_api token)
gitboard show group/project                 # the board as a tree
gitboard show group/project "Dev Board"     # a named board
gitboard show --all / -n 20                 # untruncated / 20 per column
gitboard show -m                            # stable markdown, for pipes and the AI
gitboard show --from boards/x.yaml          # render the YAML as a board, no network
gitboard pull group/project                 # live board -> boards/<name>.yaml
gitboard pull group/project --base          # ...plus an untouched boards/<name>.yaml.base
gitboard pull group/project --force         # overwrite an existing file
gitboard pull group/project --notes         # also pull each issue's discussion (read-only)
gitboard pull group/project --force --discard-edits   # overwrite even with unapplied edits
gitboard pull --all                         # every board that has a boards/*.yaml
gitboard snapshot group/project             # append board state to snapshots.jsonl (--all: every board)
gitboard status                             # per board: pulled ago, staged, notes, Q: waiting, oldest in Verify, overdue, snapshot ago
gitboard tui group/project                  # interactive loop, see below
gitboard graph group/project                # who waits on whom, one tree per milestone
gitboard graph group/project -M Beta        # only the Beta milestone and what feeds it
gitboard graph group/project --html g.html  # interactive page (click a card: its chain)
gitboard graph group/project --mermaid      # flowchart LR, paste into a GitLab description
gitboard config                             # what URL/tokens resolved, and from where

# plan / write (api token for apply and migrate)
gitboard plan boards/x.yaml                 # three-way: YAML vs GitLab, with x.yaml.base as the ancestor
gitboard plan boards/x.yaml --base FILE     # a different ancestor
gitboard plan boards/x.yaml --against boards/x.yaml.base   # diff YAML against a file, no network
gitboard apply boards/x.yaml                # write it (--yes skips the prompt)
gitboard apply boards/x.yaml --ignore-drift # write even where the team moved things since the pull
gitboard land boards/x.yaml                 # plan, y/n, apply, snapshot, rotate the base (--yes, --ignore-drift)

gitboard estimate boards/x.yaml             # stage due dates from each person's finished history; local file only
gitboard estimate boards/x.yaml --history h.json  # same from a `stats --dump` file, no network
                                            # `stats`/`digest` list Tight dates and Late milestones (critical chain's estimates past the milestone due date)

# numbers and digests (read_api token; --from FILE works with no network)
gitboard stats group/project                # team markdown: open by column/epic/story/milestone, done, cycle, verify, stuck
gitboard stats group/project --dump h.json  # ...and keep the fetched history for offline reruns
gitboard stats --from h.json [--json]       # same, from the dump; --json prints the summary dict
gitboard stats group/project --weeks 8      # the trend table from reports/stats.jsonl; no network
gitboard digest group/project               # reports/<date>/<board>/: team + <user> as .md and .html, <user>.eml, graph.html, gantt.html, index.html (mails carry Gantt charts)
gitboard digest --all                       # every local board (what `make cron` runs Monday 07:00)
gitboard digest group/project --md-only     # markdown + plain-text .eml only, no HTML
gitboard migrate-comments 12 34 35          # copy #12's comments onto #34 and #35
gitboard migrate-comments 12 other/proj#7 --close-source   # cross-project, then close #12
gitboard migrate boards/x.migration.yaml    # reformat: rename/merge/drop labels, reorder/drop columns, move cards; ! rows are one-way, y/n

# local only (no GitLab)
gitboard stats --from h.json                # see above
gitboard report group/project --repo .      # what moved, from snapshots.jsonl
gitboard report group/project --since boards/x.yaml   # since that file was pulled; --all: every board
gitboard report group/project --history h.json   # + a verified column: verdicts per verifier from a stats --dump
gitboard ingest TASKS.md --into boards/x.yaml   # tasks.md -> board issues, see tasks-flow
gitboard tui --from boards/x.yaml           # offline TUI
gitboard graph --from boards/x.yaml         # the blocker graph from the YAML
```

Global flags go before the command: `--url`, `--read-token`, `--write-token`,
`--config PATH`, `-v`.

## Notes per command

`show`
: Sorts each column overdue first, then in GitLab's board order (the
  manual order the team drags, which is the YAML's `issues:` order after
  a pull), then newest, and truncates to 5 per column so a long board fits a screen and the cut never
  hides what you were looking for. An issue with two column labels appears in
  both columns and is counted once. `Backlog` is synthesised for issues with
  no column label.

`pull`
: `apply` in reverse. Refuses to clobber an existing file unless `--force`,
  and refuses even then when the file has edits that were never applied
  (`plan` against the base is non-empty); `--discard-edits` overrides.
  Pull-then-plan is always empty. Each issue carries its `iid`, which `apply`
  uses as the match key. `--base` writes a second, untouched copy as
  `<file>.base` (gitignored, not a `*.yaml`, so nothing scans it as a spec)
  and rotates the previous one to `<file>.base.old`. `--notes` adds a
  read-only `discussion:` list per issue. Every pull also appends a
  snapshot. `--all` pulls every board that has a `boards/*.yaml`.

`plan`
: A three-way merge: the YAML, the live board, and `<spec>.base` as the
  ancestor (`--base FILE` for another). Rows are `added`, `changed` (with
  `old -> new`), `skipped` (closed on GitLab: never recreated) or `drift`
  (the board changed since the base and the YAML did not: the board's value
  is kept). Without a base it is the plain two-way diff. The table lists
  notes first, then link and order rows and `blocked_by` changes, then the
  rest: what to read before saying yes goes on top.

`plan --against FILE`
: Diffs two YAML files. Never opens a connection or looks for a token.

`apply`
: Additive: creates and updates, never deletes or closes. Matches by `iid`
  when present (so a retitle in the YAML is a rename) and by title otherwise;
  titles are stripped. Labels are truly additive: labels the team added in
  the UI survive an apply that does not list them. Closed issues are skipped.
  Posts any `notes:` not already on the issue, each with a
  `*staged via gitboard*` first line. `drift` rows are refused unless
  `--ignore-drift`. Appends a snapshot afterwards.

`land SPEC`
: `plan`, a y/n on the table, `apply`, `snapshot`, then `<spec>.base` is
  rewritten to the post-apply state (old one to `.base.old`). `--yes` skips
  the prompt, `--ignore-drift` passes through. The host side of the offline
  round in one command.

`status`
: One line per board in `boards/`: when it was pulled, staged moves and
  unposted notes (against its base), the oldest issue in `Verify` and how
  long it has sat there, overdue count, and when the last snapshot was
  taken. Read-only.

`migrate-comments`
: Copies discussion oldest first with a `*from #12, by @alice on ...:*`
  header (the API cannot post as someone else). Skips system notes and
  already-copied bodies, so re-runs are safe. `--close-source` closes the
  source with a "superseded by" note.

`ingest TASKS.md --into SPEC`
: Folds a tasks markdown file (a heading per person, checkboxes) into the
  spec: open tasks land in `--column` (default `Verify`), checked ones in
  `--done` (default `Done`). Local files only. Details in {doc}`tasks-flow`.

`graph`
: Which card waits on which on the way to each milestone. The default is
  one tree per milestone, soonest due first: the root shows the due date,
  days left and how many of its cards are done (or `no cards yet` for a
  milestone planned but not tasked); under each card are its
  blockers, recursively, and a blocker already printed shows as
  `(see #9 above)`. Each line carries the assignee, `P1`..`P4`, the due
  date, `⇠ N waiting` (cards downstream of it), `★` for the longest chain
  into the milestone, and `⚑` where the board contradicts itself (the
  same flags `stats` lists, bar `no_milestone`, which would mark every
  unplanned card). Cards with blockers but no milestone go under
  a final `No milestone` root. `-M/--milestone` keeps one milestone and
  everything upstream of it; an unknown one is an error listing the known
  ones. `--html PATH` writes one self-contained page (inline SVG, no CDN,
  works air-gapped): blockers left, milestones right, closed cards faded,
  flagged cards and their edges red; click a card to light its whole
  chain, hover for details, click empty space to clear. `--mermaid`
  prints a `flowchart LR` to stdout for a GitLab description or wiki
  page. Live, it reads the board plus 30 days of history (one links
  request per issue); `--from FILE` draws a YAML with no network, where a
  same-project blocker missing from the pull counts as closed. A board
  with no blockers and no milestones says so on stderr and exits 0.

## TUI keys

`gitboard tui group/project` runs the same renderer as `show` inside a
full-screen loop; resizing redraws.

| Key | Does |
|---|---|
| `r` | reload from GitLab |
| `b` | switch board: this project's, plus any `boards/*.yaml` |
| `s` | append a snapshot |
| `e` | edit the spec in `$EDITOR` (pulled first if missing); diff on return |
| `p` | plan |
| `a` | apply, after y/n on the change table |
| `m` | migrate comments; `b` in the destination prompt picks another project; y/n to close the source |
| `g` | show or hide the guide panels for this session |
| `?` | help |
| `q` | quit |

**Card keys** stage a change into the YAML for you — no editor. Select a
card with the arrows (or `h j k l`) and press the key, or press the key and
type the card's number; then pick a value. `p` shows what is staged, `a`
writes it. The first one pulls the board into a YAML if there is none. `esc`
drops the selection; it follows its card when a reload re-sorts or moves it.

| Key | Does |
|---|---|
| `v` | move: number, then a column by number. A card in Verify does not move by key and Done / Failed are not offered — those are a `verified:` / `failed:` comment on the card |
| `u` | assign: number, then a person by number, or `t` to type a username |
| `d` | due date: number, then `YYYY-MM-DD`, `+N` days, or `e` for the [estimate](board-yaml.md) from the assignee's finished history |
| `c` | comment: number, then one line; staged under `notes:` and posted by `a` |
| `n` | new card: a title, then a column |

`e` is still there for bulk edits. An offline `(new)` card has no number
yet, so it is reached with the cursor and named by its title.

**Guide.** Pressing a key shows a panel beside the prompt: what the mode
does, a worked example, what it will and will not write. It never waits for
a keypress. On by default; off with `g` (this session), `tui --no-guide`,
`GITBOARD_GUIDE=0`, or `guide = false` in `gitboard.toml`.

`gitboard tui --from boards/x.yaml` is the offline variant: the YAML is the
board. `r` reloads the file, `e` and the card keys edit it, `p` shows the
staged diff against `x.yaml.base`. No `b`/`s`/`m`/`a`; the host applies.

## stdout / stderr

The board goes to **stdout**. Logs, spinners, and change tables go to
**stderr**. So `gitboard show -m | less` and the `/board` prompt parse clean
output, and nothing else may write to stdout. Rich drops colour when piped.

## snapshot and report

`snapshot` appends one JSON line per open issue (timestamp, columns,
assignee, due date) to `snapshots.jsonl`. `make cron` prints a crontab line
that runs it every 30 minutes.

`report` needs two snapshots in its window (`--days 7` default) and no
network: moved, appeared, closed, sat still, per-assignee tally, and a
**stuck** section: issues past their column's threshold (`Verify` 1 day,
`Doing` 3 days), oldest first. `--since boards/x.yaml` sets the window start
to that file's pull instead of `--days`. `--all` reports every board.
`--repo PATH` adds a commits column from `git log` over the same window,
matching GitLab usernames to git authors by name or email local part.

Time in column comes from the same log: `show` appends `age:Nd` to each
markdown line and ` · Verify 3d` to each tree line when `snapshots.jsonl`
is present. The age is the start of the current streak of snapshots with
the same columns, so an issue that bounced out and back is young again.
