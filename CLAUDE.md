# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
./gitboard.py show group/project        # the board, as a rich tree
./gitboard.py show group/project -m     # markdown — stable, parseable
./gitboard.py plan boards/test.yaml     # diff YAML against GitLab
./gitboard.py apply boards/test.yaml    # write it (--yes to skip the prompt)
make test                               # full suite
make lint / make fmt                    # ruff
make up seed                            # local GitLab + demo board
```

`make` alone lists targets. Everything runs through `uv run --script` with
PEP 723 inline deps — nothing to install, no venv to activate. `make venv`
exists only for editor autocomplete; the scripts ignore it.

## Architecture

`gitboard.py` is the only executable. Everything else is a module:

- `config.py` — config singleton (`get_config()`, an `lru_cache(1)`).
  `configure()` applies CLI overrides and busts the cache. Token resolution is
  **lazy** (`Config.token()`), so `--help` never touches the keychain. Tests
  must call `config.reset()` — the singleton outlives a test otherwise, and
  they must `delenv("GITLAB_URL")` because the Makefile exports it.
  Precedence is **flag > env > TOML file > default**; the file is searched at
  `--config`/`$GITBOARD_CONFIG`, then `./gitboard.toml`, then
  `~/.config/gitboard/config.toml`. It can set `url`, `project`, `board`,
  `spec` — `project`/`spec` make the CLI arguments optional. A `token` key is
  deliberately **ignored with a warning**: credentials belong in the keychain,
  not a file that can be committed. `tomllib` is stdlib, so no dependency.
- `log.py` — console + logger singletons. **`out()` is stdout, `err()` is
  stderr.** The board goes to stdout; logs, spinners, and change tables go to
  stderr. `show -m | less` must stay clean, so nothing else may write stdout.
- `client.py` — the connection, and the only place API errors become English.
  Everything user-actionable raises `GitlabProblem`; the CLI prints it as one
  line and exits 1. No tracebacks for a typo'd path.
- `board.py` — reading. `board_columns()` is why this repo exists: **no MCP
  server exposes board structure.** A board list is bound to a label and
  membership is "has that label", so the mapping is reassembled from
  `board.lists` + `project.issues`.
- `apply.py` — the only writer. Needs an `api`-scope token; `read_api` reads
  boards fine and fails here, which is the intended split.

Deliberate, not bugs: an issue with labels from two lists appears in both
columns (the web UI does the same, no tiebreak invented); `Backlog` is
synthesised for issues with no list label.

## apply.py invariants

The YAML is the source of truth. Two normalisations exist because their
absence caused real bugs, both pinned by tests — **don't remove them**:

- An unquoted `2026-09-01` is a `datetime.date` to PyYAML, which `requests`
  cannot JSON-encode. `wanted_issue` coerces to ISO.
- GitLab strips a description's trailing newline; YAML's `|` keeps it, so
  every apply reported a phantom `description` change. `norm_text` fixes both
  sides — `wanted_issue` and `current_issue` must always agree, or nothing is
  idempotent.

Issue identity is the **title**. Renaming a title creates a second issue.
Apply is additive: nothing is deleted or closed, so removing an issue from the
YAML leaves it on the board.

`seed.py` owns only the token mint and is dependency-free; the demo board is
`boards/demo.yaml` applied through the CLI. Re-running it reverts manual board
edits back to the YAML.

## Read-only

The read-only guarantee for the AI pass is the token scope (`read_api`), not
the prompt. `.claude/commands/board.md` restates it and restricts tools to
`Bash(./gitboard.py show:*)` — note `show`, not the bare script, so the
command cannot reach `apply`. If you add write capability for the AI, both
have to change together; README "Adding writes later" has the steps.

Auth: `--token`, else `GITLAB_TOKEN`, else macOS keychain item `gitlab-token`.
`GITLAB_URL` defaults to gitlab.com. `./gitboard.py config` shows what
resolved.
