---
description: Read a GitLab board, report progress, and — after a go-ahead — apply the moves.
argument-hint: <group/project> [board name]  |  boards/<file>.yaml (offline)
allowed-tools: Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli show:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli plan:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli apply:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli report:*), Edit(boards/*.yaml)
---

Run `PYTHONPATH=src .venv/bin/python -m gitboard.cli show $ARGUMENTS --markdown`
and analyse the board it prints. (Run `make install` first if .venv is missing.)

**Offline mode.** If `$ARGUMENTS` ends in `.yaml`, there is no GitLab here
(a container without network): the file *is* the board. Run
`show --from $ARGUMENTS --markdown` instead, and for the staged diff
`plan $ARGUMENTS --against $ARGUMENTS.base` (if the `.base` copy is missing,
say so — you can show your edits but not a diff). Never run `apply` in
offline mode; the host does that. `iid:` values come from `pull` and are
informational: identity is still the title, so never retitle, never invent or
edit an `iid`, and leave it off issues you add. `report` needs only
`snapshots.jsonl`, so it works offline too when the file is present.

**The board is the conversation.** A pulled YAML may carry `discussion:`
per issue (the team's comments, read-only — never edit it). You answer by
appending to that issue's `notes:` — plain strings, one per comment; `apply`
posts each once and skips any body already on the issue. Address feedback
there, not in a tasks file.

You have write access, through exactly one path: edit the YAML in `boards/`
that defines the board, run `plan`, show its pending table, and wait for a
go-ahead in this conversation; on a yes, run `apply --yes`. Never run `apply`
whose `plan` output the user has not just seen. No other write path — no MCP
write tools, no direct API calls. `apply` is additive-only: it never deletes
or closes anything, and issues are matched by title, so never retitle an
issue in the YAML (that creates a second issue).

Report exactly these four sections (five offline), and keep each one short:

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

Replies you staged under `notes:` count as moves: list them as
`#iid: note — first line…`.

If one issue looks superseded by another (retitled duplicate, split work,
a stale twin of a newer issue), suggest the migration as a copyable line —
`gitboard migrate-comments OLD NEW --close-source` — for the user to run.
You cannot run it yourself, and should not try.

Then offer to apply them: make the YAML edit, run `plan`, show the table, and
ask. An issue missing from the YAML can be added to it (title must match the
board exactly). If the user declines, leave the YAML as you found it.

## Hand back
Offline mode only. Once the edits are staged in the YAML, the user copies it
back to the host and runs there, in this order:
`gitboard plan boards/<file>.yaml` (live — shows anything that drifted since
the pull) then `gitboard apply boards/<file>.yaml`. List any
`migrate-comments` lines under it. Nothing you did here has touched GitLab.

Ground every claim in an issue number from the output. If the board is empty
or the script errors, say so and stop — do not infer a board from the repo.
