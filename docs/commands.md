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
gitboard snapshot group/project             # append board state to snapshots.jsonl
gitboard tui group/project                  # interactive loop, see below
gitboard config                             # what URL/tokens resolved, and from where

# plan / write (api token for apply and migrate)
gitboard plan boards/x.yaml                 # diff YAML against GitLab
gitboard plan boards/x.yaml --against boards/x.yaml.base   # diff YAML against a file, no network
gitboard apply boards/x.yaml                # write it (--yes skips the prompt)
gitboard migrate-comments 12 34 35          # copy #12's comments onto #34 and #35
gitboard migrate-comments 12 other/proj#7 --close-source   # cross-project, then close #12

# local only (no GitLab)
gitboard report group/project --repo .      # what moved, from snapshots.jsonl
gitboard ingest TASKS.md --into boards/x.yaml   # tasks.md -> board issues, see tasks-flow
gitboard tui --from boards/x.yaml           # offline TUI
```

Global flags go before the command: `--url`, `--read-token`, `--write-token`,
`--config PATH`, `-v`.

## Notes per command

`show`
: Sorts each column overdue first, then soonest due, then newest, and
  truncates to 5 per column so a long board fits a screen and the cut never
  hides what you were looking for. An issue with two column labels appears in
  both columns and is counted once. `Backlog` is synthesised for issues with
  no column label.

`pull`
: `apply` in reverse. Refuses to clobber an existing file unless `--force`.
  Pull-then-plan is always empty. Each issue carries its `iid`, which `plan`
  and `apply` ignore. `--base` writes a second, untouched copy as
  `<file>.base` (gitignored, not a `*.yaml`, so nothing scans it as a spec).
  `--notes` adds a read-only `discussion:` list per issue.

`plan --against FILE`
: Diffs two YAML files. Never opens a connection or looks for a token.

`apply`
: Additive: creates and updates, never deletes or closes. Matches issues by
  title. Posts any `notes:` that are not already on the issue.

`migrate-comments`
: Copies discussion oldest first with a `*from #12, by @alice on ...:*`
  header (the API cannot post as someone else). Skips system notes and
  already-copied bodies, so re-runs are safe. `--close-source` closes the
  source with a "superseded by" note.

`ingest TASKS.md --into SPEC`
: Folds a tasks markdown file (a heading per person, checkboxes) into the
  spec: open tasks land in `--column` (default `Verify`), checked ones in
  `--done` (default `Done`). Local files only. Details in {doc}`tasks-flow`.

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
| `?` | help |
| `q` | quit |

`gitboard tui --from boards/x.yaml` is the offline variant: `r` reloads the
file, `e` edits it, `p` shows the staged diff against `x.yaml.base`, `?`
help, `q` quit. No GitLab keys.

## stdout / stderr

The board goes to **stdout**. Logs, spinners, and change tables go to
**stderr**. So `gitboard show -m | less` and the `/board` prompt parse clean
output, and nothing else may write to stdout. Rich drops colour when piped.

## snapshot and report

`snapshot` appends one JSON line per open issue (timestamp, columns,
assignee, due date) to `snapshots.jsonl`. `make cron` prints a crontab line
that runs it every 30 minutes.

`report` needs two snapshots in its window (`--days 7` default) and no
network: moved, appeared, closed, sat still, per-assignee tally.
`--repo PATH` adds a commits column from `git log` over the same window,
matching GitLab usernames to git authors by name or email local part.
