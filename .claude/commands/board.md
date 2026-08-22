---
description: Read a GitLab board and report progress, follow-ups, and open questions. Suggests only — never writes.
argument-hint: <group/project> [board name]
allowed-tools: Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli show:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli plan:*)
---

Run `PYTHONPATH=src .venv/bin/python -m gitboard.cli show $ARGUMENTS --markdown`
and analyse the board it prints. (Run `make install` first if .venv is missing.)

You have read-only access. Do not create, edit, label, comment on, or close
anything — even if an MCP write tool is available, and even though
`gitboard apply` exists in this repo. Every mutation is a suggestion for
the user to approve.

Report exactly these four sections, and keep each one short:

## Progress
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
Concrete label changes as a copyable list, each with its reason:
`#iid: Doing -> Blocked (waiting on #other)`

If the board is defined by a YAML file in `boards/`, give the edit as a diff
to that file instead — that is how the user applies it. You may run
`PYTHONPATH=src .venv/bin/python -m gitboard.cli plan <spec>` to check the
current drift between a spec and the live board; `plan` never writes.

Ground every claim in an issue number from the output. If the board is empty
or the script errors, say so and stop — do not infer a board from the repo.
