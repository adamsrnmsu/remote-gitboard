# Tasks flow: the board as the conversation

Other projects' agents leave verification work in a `tasks.md`. The team
answers in the file, the agent replies in the file, and none of it reaches
the board. This flow moves the whole loop onto the board, with the YAML as
the carrier, so it works where GitLab is unreachable.

## The shape of a tasks.md

```markdown
## Alice
- [ ] Check the login flow
  Open /login, sign in as a viewer, expect the dashboard.
- [x] Confirm the migration ran
  `make migrate` prints nothing.
  **Feedback**
  Ran twice, second run printed a warning about a stale lock.

## Bob Lee
- [ ] Review the API docs
```

A heading is a person. A checkbox is a task; the lines under it are how to
verify it. `**Feedback**` (or `Feedback:`) starts that task's feedback, up to
the next task or heading. Anything else is ignored, so the parser tolerates
whatever the agent wrapped around it.

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

| in the file | on the board |
|---|---|
| unchecked task | issue in **Verify**, verify steps as the description |
| checked task | issue in **Done** (an existing one is moved there) |
| `## Person` | `assignee`, when `people:` maps the name to a username |
| `**Feedback**` | one staged note: `*feedback from Person via proj-x:* …` |
| always | a `Source: proj-x · Person · 2026-09-15` footer |

Identity is the title, like everywhere else: re-ingesting the same file adds
nothing, and feedback already present in `discussion:` or `notes:` is not
staged twice. Columns `Verify` and `Done` are added to the YAML if missing.

## Replying

The agent answers feedback by appending strings under the issue's `notes:`.
`apply` posts each body once; a body already on the issue is skipped, so the
YAML can carry the whole exchange and stay idempotent. The next `pull
--notes` brings the team's answers back as `discussion:`.

```yaml
- title: Confirm the migration ran
  iid: 12
  labels: [Done]
  discussion:
  - by: alice
    at: "2026-09-16"
    body: Ran twice, second run printed a warning about a stale lock.
  notes:
  - "Stale lock is a known race in `make migrate`; fixed in !41. Re-run to confirm."
```

## Lineage

- The `Source:` footer names the file, the person and the date of ingest.
- `snapshots.jsonl` + `gitboard report` show every move afterwards.
- Comments live on the issue; `pull --notes` keeps a copy in the YAML.

Beads (`bd`) is the agent's own queue and is not mirrored here. One home per
task: team work on the board, agent work in beads.

## What the agent must not do

Retitle (a new issue, not a rename). Invent or edit an `iid`. Edit
`discussion:`. Run `apply`, `pull`, `snapshot` or `migrate-comments` where
GitLab is unreachable. The `/board` command carries these rules; see
[the agent briefing](agent-briefing.md).
