# Tasks flow: the board as the conversation

Other projects' agents leave verification work in a `tasks.md`. The team
answers in the file, the agent replies in the file, and none of it reaches
the board. This flow moves the whole loop onto the board, with the YAML as
the carrier, so it works where GitLab is unreachable.

The rule that makes the board truthful: **a task moves only on a person's
word.** A comment on the issue whose first line is `verified` moves it to
Done; `failed` moves it to Failed. A `[x]` in the file moves nothing.
Agents write the file; people write the verdicts.

## The shape of a tasks.md

```markdown
commit: 9f3c2a1
mr: !41

## Alice
- [ ] Check the login flow · id: T-login
  Open /login, sign in as a viewer, expect the dashboard.
  evidence: screenshots/login.png
- [ ] Confirm the migration ran · id: T-migrate
  `make migrate` prints nothing.
  **Feedback**
  Ran twice, second run printed a warning about a stale lock.

## Bob Lee
- [ ] Review the API docs
```

Header lines name the commit, MR or session the work came from. A level-2
heading is a person; a checkbox is a task; the lines under it are how to
verify it; `**Feedback**` starts the person's reply. The full grammar, with
a paste-ready block for the agent's briefing, is the
[tasks.md contract](tasks-md-contract.md).

## One round

```bash
# host
gitboard pull group/project --base --notes      # boards/x.yaml (+ .base), with comments
# -> copy boards/ and any tasks.md into the container

# container (the agent runs these; you review its plan table)
gitboard ingest tasks.md --into boards/x.yaml --source proj-x
gitboard plan boards/x.yaml --against boards/x.yaml.base
# -> copy boards/x.yaml back

# host
gitboard plan boards/x.yaml                     # live: also shows drift since the pull
gitboard apply boards/x.yaml
```

`ingest` does, per task:

| in the file / on the issue | on the board |
|---|---|
| new task | issue in **Verify**; verify steps as a task list, `Evidence:` line, `Source:` footer |
| latest comment starting `verified` | moved to **Done** |
| latest comment starting `failed` | moved to **Failed** |
| `[x]` with no verdict comment | not moved; listed as *unverified* |
| `## Person` | `assignee`, when `people:` maps the name to a username |
| `**Feedback**` | one staged note: `*feedback from @alice via proj-x:* …` |
| header `commit:` differs from the footer's | footer rewritten, label `re-verify` until a newer verdict |
| task with this `--source` missing from the file | label `stale`; nothing removed or moved |
| task returns to the file | `stale` dropped |

A `verified` comment is still checked afterwards: `stats` and the digests
list it under **Weak verdicts** when the issue's verify steps are not all
ticked (`steps 1/4`) or the comment came within ten minutes of the card
entering Verify. Nothing moves and the verdict stands; tick the steps you
did run and the flag clears on the next run.

Columns are added to the YAML as needed: `Verify` (carrot orange), `Done`
(medium sea green), `Failed` (crimson); `--column`, `--done`, `--failed`
rename them.

Identity is the footer `id:` first, then the exact title. An id whose
title changed keeps the board title and is reported (`retitled`); a new
title within 85 % of an existing one is reported (`similar`) and not
added. Re-ingesting the same file changes nothing and leaves the YAML
untouched; feedback already present in `discussion:` or `notes:` is not
staged twice.

## Lineage

Every issue carries one footer line, rewritten in place when the commit
moves:

```text
Source: proj-x · Alice · 2026-09-15 · id: T-login · commit: 9f3c2a1 · mr: !41 · session: run-7
```

`source`, person and date are fixed; `id:`, `commit:`, `mr:`, `session:`
are optional. `re-verify` is cleared by a verdict dated on or after the
footer date, so a `verified` from before the commit bump does not count.

`snapshots.jsonl` + `gitboard report` show every move afterwards; comments
live on the issue and `pull --notes` keeps a copy in the YAML.

## Replying

The agent answers feedback by appending strings under the issue's `notes:`.
`apply` posts each body once; a body already on the issue is skipped, so the
YAML can carry the whole exchange and stay idempotent. The next `pull
--notes` brings the team's answers back as `discussion:` — including the
verdicts, which the next `ingest` acts on.

```yaml
- title: Confirm the migration ran
  iid: 12
  labels: [Failed]
  discussion:
  - by: alice
    at: "2026-09-16"
    body: |
      failed: second run still warns about the stale lock.
  notes:
  - "Stale lock is a known race in `make migrate`; fixed in !41. Re-run to confirm."
```

Beads (`bd`) is the agent's own queue and is not mirrored here. One home per
task: team work on the board, agent work in beads.

## What the agent must not do

Tick a box (the verdict is the person's). Retitle without an `id:` (a new
issue, not a rename). Invent or edit an `iid`. Edit `discussion:`. Run
`apply`, `pull`, `snapshot` or `migrate-comments` where GitLab is
unreachable. The `/board` command carries these rules; see
[the agent briefing](agent-briefing.md).
