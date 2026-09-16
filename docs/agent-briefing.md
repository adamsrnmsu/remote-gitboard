# What to tell the agent

Three paste-ready blocks for the container in {doc}`airgap`. Replace `x`
with the board's file name.

## 1. Project `CLAUDE.md` for the container

```markdown
# Board work, offline

There is no GitLab here and no token. The board is `boards/x.yaml`
(`boards/x.yaml.base` is the untouched pull). Use `/board boards/x.yaml`.

Rules:
- Never run `apply`, `migrate-comments`, `snapshot`, or `pull`. They need
  GitLab and the host runs them.
- Stage every change in the YAML only. `plan boards/x.yaml --against
  boards/x.yaml.base` shows what you staged.
- Never retitle an issue: the title is its identity.
- Never invent or edit an `iid`. New issues carry no `iid`.
- Reply to team feedback (`discussion:`) by adding `notes:` on that issue.
- Your own work is beads: `bd ready`, `bd update <id> --claim`, `bd close <id>`.
- At the end, list the host commands: `gitboard plan boards/x.yaml`,
  `gitboard apply boards/x.yaml`, `bd import issues.jsonl`, and any
  `migrate-comments` lines.
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
