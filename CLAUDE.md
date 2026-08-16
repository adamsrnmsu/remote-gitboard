# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
./apply.py boards/test.yaml --dry-run   # preview YAML -> GitLab (never writes)
./apply.py boards/test.yaml             # write it
./board.py group/project [board-name]   # dump a board as markdown
make test                               # pytest + selftest
uvx pytest -q -k backlog                # a single test
./board.py --selftest                   # dep-free check, runs with nothing installed
docker compose up -d && ./seed.py       # local GitLab CE + demo board (first boot 5-10 min)
docker compose down -v                  # reset the local instance
```

`make` alone lists targets; `make up seed` is the whole cold start.
`make fmt` / `make lint` run ruff via `uvx` — nothing to install.

Both scripts are `uv run --script` shebangs with inline PEP 723 deps
(`python-gitlab`) — no venv, no requirements file. Run them directly, not via
`python board.py`. `pyproject.toml` is ruff config only; the project is not a
package and `make venv` exists for editor autocomplete, nothing else.

`_selftest`'s stub classes are wrapped in `# fmt: off` on purpose — the
formatter expands them to 35 lines and buries the assertions. Leave it.

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

## apply.py — the only writer

`board.py` reads, `apply.py` writes, and they share `token()`/`URL` so there
is one auth path. `apply.py` needs an `api`-scope token; a `read_api` token
reads boards fine and fails here, which is the intended split.

The YAML is the source of truth. Two normalisations exist because their
absence caused real bugs, both pinned by tests — **don't remove them**:
- An unquoted `2026-09-01` is a `datetime.date` to PyYAML, which `requests`
  cannot JSON-encode. `wanted_issue` coerces to ISO.
- GitLab strips a description's trailing newline; YAML's `|` keeps it, so
  every apply reported a phantom `description` change. `norm_text` fixes both
  sides — `wanted_issue` and `current_issue` must always agree, or nothing is
  idempotent.

Issue identity is the **title**. Renaming a title in YAML creates a second
issue rather than renaming the first. Apply is additive: nothing is deleted
or closed, so removing an issue from the YAML leaves it on the board.

`seed.py` owns only the token mint now; the demo board lives in
`boards/demo.yaml` and goes through `apply.py`. Re-running `seed.py` reverts
manual board edits back to the YAML.

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
