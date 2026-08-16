# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
./board.py group/project [board-name]   # dump a board as markdown
./board.py --selftest                   # the only test; runs with no deps installed
docker compose up -d && ./seed.py       # local GitLab CE + demo board (first boot 5-10 min)
docker compose down -v                  # reset the local instance
```

Both scripts are `uv run --script` shebangs with inline PEP 723 deps
(`python-gitlab`) — no venv, no requirements file. Run them directly, not via
`python board.py`.

## Architecture

Four files, each with one job (see README's table). The whole point of the repo
is `board_columns()` in `board.py:33`: **no MCP server exposes GitLab board
structure.** A board list is bound to a label and membership is "has that
label", so the column mapping has to be reassembled from `board.lists` +
`project.issues`. Everything else — issues, comments, labels — is already
covered by the official GitLab MCP server, `python-gitlab`, or `glab`; don't
reimplement it here.

Consequences that are deliberate, not bugs:
- An issue with labels from two lists appears in both columns. The GitLab web UI
  does the same; no tiebreak is invented.
- `Backlog` is synthesised for issues carrying no list label.
- `gitlab` is imported inside `main()` so `--selftest` works dependency-free.
  Keep it that way.

## Read-only

The read-only guarantee is the token scope (`read_api`), not the prompt. The
`/board` command in `.claude/commands/board.md` restates it and restricts tools
to `Bash(./board.py:*)`. If you add write capability, both have to change
together — README "Adding writes later" has the steps.

Auth: `GITLAB_TOKEN` env var, else macOS keychain item `gitlab-token`.
`GITLAB_URL` defaults to gitlab.com.

## seed.py

Throwaway-instance only — it shells into the container and mints a root PAT via
the Rails console (the API can't bootstrap its own first token). Everything
after that step goes through the REST API on purpose: the API is stable across
GitLab versions, Rails internals are not. The fixed token value keeps re-seeds
from invalidating your keychain entry.
