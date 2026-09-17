---
description: Read a GitLab board, stage the moves it needs, and — after a go-ahead — apply them.
argument-hint: <group/project> [board name]  |  boards/<file>.yaml (offline)
allowed-tools: Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli show:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli plan:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli apply:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli report:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli ingest:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli status:*), Edit(boards/*.yaml)
---

`gitboard` below is `PYTHONPATH=src .venv/bin/python -m gitboard.cli`
(run `make install` first if `.venv` is missing).

**Read.** `gitboard show $ARGUMENTS --markdown` is the board. If
`snapshots.jsonl` exists, `gitboard report $ARGUMENTS` adds what moved and
its **stuck** section; `show` lines then carry an `age:Nd` suffix (days in
the current column). `gitboard status` gives the per-board summary line.

**Offline.** If `$ARGUMENTS` ends in `.yaml` there is no GitLab here: the
file is the board. Use `show --from $ARGUMENTS --markdown` and
`plan $ARGUMENTS --against $ARGUMENTS.base` (no `.base`: say so; you can
list your edits but not a diff). Never run `apply`, `land`, `pull` or
`snapshot`; the host does. `report` works if `snapshots.jsonl` was copied in.

**First, ingest.** If a `tasks.md` was dropped in, run
`gitboard ingest tasks.md --into <spec>` before anything else, then review
what it added like any other edit. See `docs/tasks-md-contract.md` for what
it reads (`id: T-slug`, `commit:`/`mr:` header, `- [ ]` verify steps,
`@mention` feedback).

**Write path.** Exactly one: edit the board's YAML in `boards/`, run
`gitboard plan <spec>`, show its table, wait for a yes in this conversation,
then `gitboard apply <spec> --yes`. Never an `apply` whose `plan` the user
has not just seen; never `land`, MCP write tools or direct API calls.

Rules for the YAML:

- You may move an issue **into** `Verify`. Never out of it: `Done` and
  `Failed` come from a person's `verified: ...` / `failed: ...` comment
  (`ingest` reads them). A `[x]` in a tasks.md is advisory, not a verdict.
- Never retitle an entry without an `iid`. With one, a new title is a
  **rename** of that issue: allowed, but the Staged row must say "rename".
- Never edit `discussion:` (the team's comments, read-only). Reply by
  appending a string to that issue's `notes:`; `apply` posts each once with
  a `*staged via gitboard*` first line.
- Never invent or edit an `iid`; new issues carry none. Never delete or
  close anything; closed issues are skipped by `apply`, not recreated.
- Labels you add are additive; labels the team added in the UI survive.
  `stale` (no movement past the threshold) and `re-verify` (changed after
  verification) are the follow-up labels: add them, do not invent others.
- Every `drift` or `skipped` row in the plan table gets a one-line reason in
  Staged. Never drop a row.
- Supersession (a retitled twin, split work): suggest the copyable
  `gitboard migrate-comments OLD NEW --close-source` line. You cannot run it.

**Report.** Line 1 is one status line, nothing above it:

`open N · Verify N (oldest Nd) · overdue N · staged N moves · N unposted notes · N questions`

Then only the sections that are non-empty, in this order, each short:

## Questions
Only a question that blocks a staged move. Otherwise stage the default and
say "reverse if wrong". Each question is also staged as a `notes:` entry on
that issue starting `Q:`, so the answer comes back through `discussion:` on
the next pull. Ground each in an `#iid`.

## Staged
The `plan` table verbatim, then one reason per row
(`#12: Doing -> Verify — MR !40 merged`; `#7: rename — was "Fix login"`;
`#3: skipped — closed on GitLab`; `#9: drift — team moved it to Blocked
after the pull, kept theirs`). Notes count as rows: `#12: note — first line`.

## Stuck
From `report`'s stuck section and the `age:` suffixes: `#iid — column,
days, what would unstick it`, plus the label you staged (`stale`).

## Hand back
Offline only, three lines: the file to copy back, then
`gitboard land boards/<file>.yaml` on the host (plan, y/n, apply, snapshot,
rotate base), then any `migrate-comments` lines.

Ground every claim in an issue number from the output. If the board is
empty or the script errors, say so and stop; do not infer a board from the
repo.
