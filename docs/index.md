# gitboard

Read a self-hosted GitLab issue board from the terminal. Define it in YAML.
Let an AI pass report on it, and stage its moves as a diff you approve.

**The model in two sentences.** The YAML in `boards/` is the source of truth:
you edit it, `plan` shows the drift, `push` writes it. `push`
never deletes an issue: `closed: true` closes one, and a file edit cannot
delete.

```{toctree}
:maxdepth: 1

install
commands
board-yaml
ai-pass
airgap
agent-briefing
tasks-flow
tasks-md-contract
migrations
development
reference
laya-bench
```
