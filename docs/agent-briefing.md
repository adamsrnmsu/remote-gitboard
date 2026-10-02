# What to tell the agent

Three paste-ready blocks for the container in {doc}`airgap`. Replace `x`
with the board's file name.

## 1. Project `CLAUDE.md` for the container

```markdown
# Board work, offline

There is no GitLab here and no token. The board is `boards/x.yaml`
(`boards/x.yaml.base` is the untouched pull). Use `/board boards/x.yaml`.

Rules:
- Never run `push`, `migrate-comments`, `snapshot`, or `pull`. They need
  GitLab and the host runs them.
- Stage every change in the YAML only. `plan boards/x.yaml --against
  boards/x.yaml.base` shows what you staged.
- You may move an issue into `Verify`, never out of it. `Done` and
  `Failed` come from a person's `verified:` / `failed:` comment, which
  `ingest` reads; a `[x]` in a tasks.md is advisory.
- Never retitle an entry without an `iid` (that makes a second issue). With
  an `iid` it is a rename; say so.
- Never invent or edit an `iid`. New issues carry no `iid`.
- Reply to team feedback (`discussion:`, read-only) by adding `notes:` on
  that issue. A question is a note starting `Q:`.
- `stale` and `re-verify` are the follow-up labels; add those, invent none.
- A dropped `tasks.md` is ingested first: `gitboard ingest tasks.md --into
  boards/x.yaml`. Its shape is docs/tasks-md-contract.md.
- Your own work is beads: `bd ready`, `bd update <id> --claim`, `bd close <id>`.
- At the end, list the host commands: `gitboard sync boards/x.yaml`,
  `bd import issues.jsonl`, and any `migrate-comments` lines.
```

## What the team needs to know

Eight lines for the people whose cards the agent touches. Paste into the
project README or pin it on the board.

```markdown
- A card in **Verify** is waiting for you. Check it, then comment
  `verified: <what you saw>` or `failed: <what went wrong>` on the card.
  That comment is what moves it to Done or Failed; ticking a box does not.
- Do not retitle cards. The title is how the agent finds them.
- Labels you add in the UI survive. The agent only ever adds labels.
- Closing a card is final: the agent never reopens or recreates it.
- A comment starting `Q:` (posted as the gitboard bot, marked
  *staged via gitboard*) is a question for you. Reply under it.
- `stale` means it sat too long; `re-verify` means it changed after you
  verified it. Both are the agent asking for another look.
```

## 2. Session-start prompt

```text
Read boards/x.yaml and boards/x.yaml.base. Run /board boards/x.yaml.
Then: ingest any tasks.md I dropped in, propose moves, stage them in the
YAML, and show me `plan boards/x.yaml --against boards/x.yaml.base`.
```

## 3. Land the plane

```text
Before you stop:
- [ ] File a bead for anything left unfinished (`bd create`), close what is done.
- [ ] Run `make test`.
- [ ] `bd export --all -o issues.jsonl`
- [ ] `gitboard plan boards/x.yaml --against boards/x.yaml.base` one last time.
- [ ] List what goes back to the host: boards/x.yaml, issues.jsonl, and the
      exact host commands in order.
```
