---
description: Read a GitLab board, report progress, and — after a go-ahead — apply the moves.
argument-hint: <group/project> [board name]
allowed-tools: Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli show:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli plan:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli apply:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli report:*), Edit(boards/*.yaml)
---

Run `PYTHONPATH=src .venv/bin/python -m gitboard.cli show $ARGUMENTS --markdown`
and analyse the board it prints. (Run `make install` first if .venv is missing.)

You have write access, through exactly one path: edit the YAML in `boards/`
that defines the board, run `plan`, show its pending table, and wait for a
go-ahead in this conversation; on a yes, run `apply --yes`. Never run `apply`
whose `plan` output the user has not just seen. No other write path — no MCP
write tools, no direct API calls. `apply` is additive-only: it never deletes
or closes anything, and issues are matched by title, so never retitle an
issue in the YAML (that creates a second issue).

Report exactly these four sections, and keep each one short:

## Progress
If a `snapshots.jsonl` exists, first run
`PYTHONPATH=src .venv/bin/python -m gitboard.cli report $ARGUMENTS` and
ground this section in actual movement — what changed, what sat still —
rather than the current shape alone. If it reports too few snapshots, fall
back to the board as it stands.
Where the work actually stands. Column counts are the least interesting
signal — say what moved, what the shape of the board implies, and whether the
distribution looks healthy or lopsided.

## Needs follow-up
Issues that have gone quiet, sit in a column that no longer matches their
state, are unassigned in an active column, are past `due:`, or carry a
blocking label with no visible unblocker. For each: `#iid — one line why`,
plus the label you'd add. Do not add it.

## Questions for you
Things the board cannot answer and you would genuinely need a human for —
ambiguous ownership, an issue whose title contradicts its column, scope that
looks like it drifted. Ask only what changes what you'd recommend next.
No questions is a valid answer. Do not manufacture them to fill the section.

## Suggested moves
Concrete label changes, each with its reason:
`#iid: Doing -> Blocked (waiting on #other)`

Then offer to apply them: make the YAML edit, run `plan`, show the table, and
ask. An issue missing from the YAML can be added to it (title must match the
board exactly). If the user declines, leave the YAML as you found it.

Ground every claim in an issue number from the output. If the board is empty
or the script errors, say so and stop — do not infer a board from the repo.
