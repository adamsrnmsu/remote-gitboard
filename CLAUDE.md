# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
make install                            # once: venv + dependencies
make show PROJECT=group/project         # the board, as a rich tree
make plan SPEC=boards/test.yaml         # diff YAML against GitLab
make apply SPEC=boards/test.yaml        # write it
PYTHONPATH=src .venv/bin/python -m gitboard.cli show grp/proj -m   # markdown
make test                               # full suite
make lint / make fmt                    # ruff, from .venv
make up seed                            # local GitLab + demo board
```

`make` alone lists targets.

Plain venv + pip; **no uv anywhere** — it was removed deliberately, do not
reintroduce it.

**Never use an editable install on this machine.** `pip install -e .` works
through a `.pth` in `.venv` that adds `src/`; it stops being honoured ~8
seconds after install, with the file present and readable, its target
existing, and `site` listing it in the directory. Root cause unknown. Every
make target therefore runs `PYTHONPATH=src .venv/bin/python -m gitboard.cli`,
which names `src` directly and makes edits live with no reinstall.
`make link` writes a `~/.local/bin/gitboard` wrapper doing the same. Do not
"simplify" the PYTHONPATH away.

## Architecture

`src/` layout, package `gitboard`, entry point `gitboard.cli:app` declared in
`pyproject.toml`. `scripts/seed.py` is plain python3 (stdlib only) and shells
out to `.venv/bin/python -m gitboard.cli`.

Modules:

- `config.py` — config singleton (`get_config()`, an `lru_cache(1)`).
  `configure()` applies CLI overrides and busts the cache. Token resolution is
  **lazy** (`Config.token()`), so `--help` never touches the keychain. Tests
  must call `config.reset()` — the singleton outlives a test otherwise, and
  they must `delenv("GITLAB_URL")` because the Makefile exports it.
  Precedence is **flag > env > `.env` > TOML file > default**; the file is searched at
  `--config`/`$GITBOARD_CONFIG`, then `./gitboard.toml`, then
  `~/.config/gitboard/config.toml`. It can set `url`, `project`, `board`,
  `spec` — `project`/`spec` make the CLI arguments optional. A `token` key is
  deliberately **ignored with a warning**: credentials belong in `.env` or the
  keychain, not a file meant to be shared. `tomllib` is stdlib, so no
  dependency there; `.env` uses python-dotenv (cwd, then `config.HERE`).
  Tests must isolate `config.HERE` and `XDG_CONFIG_HOME` or the repo's own
  `.env` and the developer's user config leak in — the autouse fixture in
  `tests/test_config.py` does this, and twelve tests failed before it did.
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
- `apply.py` — the only writer. Uses `Config.token(write=True)`: the separate
  `GITLAB_WRITE_TOKEN` / `--write-token` / `gitlab-write-token` keychain slot,
  falling back to the read token when unset (a single `api` token is a valid
  setup). `client.write_errors()` turns the resulting 401/403 into a message
  naming the scope. Two slots exist so a `read_api` token can be the only one
  the read-only AI pass can reach.

Deliberate, not bugs: an issue with labels from two lists appears in both
columns (the web UI does the same, no tiebreak invented); `Backlog` is
synthesised for issues with no list label.

`board_columns` sorts by `_urgency` (overdue, then soonest due, then newest).
The API's order is not stable, and a truncated column has to show what the
reader would have gone looking for. `summarise` de-duplicates by iid — a
two-column issue is one issue, and summing per-column counts double-counts it.
`show` truncates to 5 per column by default; `--all` / `-n` override.

Config paths (`gitboard.toml`) and `.env` both walk up from the cwd. When only
`.env` did, running from `boards/` silently lost the repo config. A relative
`spec` in a config file resolves against **that file's** directory, not the
cwd, for the same reason.

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
`Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli show:*)` — note `show`, so it
cannot reach `apply`. If you add write capability for the AI, both
have to change together; README "Adding writes later" has the steps.

Auth (read): `--read-token`, else `GITLAB_READ_TOKEN`, else keychain
`gitlab-read-token`. `GITLAB_TOKEN` and the `gitlab-token` keychain item are
honoured as pre-rename fallbacks; the env var warns.
Auth (write): `--write-token`, else `GITLAB_WRITE_TOKEN`, else keychain
`gitlab-write-token`, else the read token.
`GITLAB_URL` defaults to gitlab.com. `make show`'s sibling `config` command
shows what
resolved.
