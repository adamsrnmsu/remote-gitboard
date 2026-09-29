---
description: Read a GitLab board, stage the moves it needs, and — after a go-ahead — apply them.
argument-hint: <group/project> [board name]  |  boards/<file>.yaml (offline)
allowed-tools: Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli show:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli plan:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli apply:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli report:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli ingest:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli status:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli stats:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli estimate:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli graph:*), Edit(boards/*.yaml)
---

`gitboard` below is `PYTHONPATH=src .venv/bin/python -m gitboard.cli`
(run `make install` first if `.venv` is missing).

**Read.** `gitboard show $ARGUMENTS --markdown` is the board. If
`snapshots.jsonl` exists, `gitboard report $ARGUMENTS` adds what moved and
its **stuck** section; `show` lines then carry an `age:Nd` suffix (days in
the current column). `gitboard status` gives the per-board summary line.
`gitboard stats $ARGUMENTS` (or `stats --from history.json` offline) is the
team's numbers: open by column/epic/story, done this week, cycle time,
verify queue and times, coverage, stuck, questions waiting, and the
blocker flags (below). Quote them; do not recompute counts by hand.
`show` lists overdue cards first, then GitLab's board order: the order
the team works in.

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

**Estimate.** `gitboard estimate <spec>` (offline: `--history h.json`, a
`stats --dump` file) stages a `due_date` on assigned, undated cards from
that person's finished history and prints the basis per card. Run it after
your own edits and before `plan`; quote the basis lines in your summary.
Never hand-write a due date the tool declined to give — "no estimate" means
not enough history. Never change a date a person set; if `stats` lists it
under *Tight dates*, say so and let the lead decide. With
`estimates: {suggest_due: false}` in the spec it only prints.

**Blockers, milestones, priority, order.** `gitboard graph --from <spec>`
(or `graph $ARGUMENTS` online; `-M <milestone>` narrows it) draws which
card waits on which on the way to each milestone; `⇠ N waiting` is how
many cards sit downstream, `★` the longest chain into a milestone, `⚑` a
contradiction. The flags are in `stats` under Flow: `blocked_stale`,
`blocked_unmarked`, `priority_inversion`, `date_inversion`,
`unowned_blocker`. They are flags, never moves.

- You may stage `milestone:` on a card, entries under `milestones:`
  (title, `due_date`, `description`), `priority::N` labels (one per card,
  1 is most urgent), and `blocked_by:` additions and removals (an iid, a
  `group/project#iid`, or the exact title of another card in the spec).
- You may reorder entries within `issues:`: list order is the board order.
  It only takes effect when the spec has a `.base` (it was pulled); on a
  hand-written spec say that the reorder will not apply.
- For each flag, propose one fix in Staged: raise the blocker's priority,
  move it up the list, pull its date in, assign it, or move the card into
  or out of Blocked (never out of Verify).
- Never edit a `Blocked by:` footer line in a description, and do not
  drop a ref only the footer holds from `blocked_by`: `plan` shows it as a
  change, but `apply` skips it and it comes back on every plan. Name it
  for the lead to edit in GitLab instead.
- Link removals and order moves go first in your summary, right after
  notes — they are the rows the lead reads before saying yes.

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
- Scoped labels are the vocabulary `stats` reads: `epic::<name>`,
  `story::<name>`, `type::<bug|task|chore|verify>`. Keep them on cards you
  touch; for an unlabelled card, propose one in Staged (reuse names that
  already exist on the board; never coin a near-duplicate). One `epic::`
  and one `story::` per card — `stats` flags doubles as `multi_scope`.
- Every `drift` or `skipped` row in the plan table gets a one-line reason in
  Staged. Never drop a row.
- A reformat (labels to merge or rename, a column to retire or reorder,
  cards that belong on another board) is not a YAML edit: say so in one
  line and point at `/migrate-board`. `gitboard migrate` is not yours to run.
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
The `plan` table verbatim (it lists notes, then link and order rows,
then the rest), then one reason per row, in the same order
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
