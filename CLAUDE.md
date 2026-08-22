# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
make install                        # once: venv + dependencies
make show PROJECT=group/project     # the board, as a rich tree
make tui PROJECT=group/project      # interactive: edit/plan/apply/migrate
make pull PROJECT=group/project     # save the live board as boards/<name>.yaml
make plan SPEC=boards/test.yaml     # diff YAML against GitLab
make apply SPEC=boards/test.yaml    # write it
make snapshot PROJECT=group/project # append board state to snapshots.jsonl
make report PROJECT=group/project   # what moved, from the snapshot log
make cron                           # print the crontab line for snapshots
make test                           # pytest
make lint / make fmt                # ruff, from .venv
make up wait install && scripts/seed.py   # local GitLab + demo board
scripts/bulk_demo.py                # 5 stress boards on the local instance
```

CLI-only (no make target): `migrate-comments SRC DST... [--close-source]` —
destinations are iids or `group/project#iid`.

`make` alone lists targets. For flags the targets don't expose, call the CLI
directly: `PYTHONPATH=src .venv/bin/python -m gitboard.cli show grp/proj -m`.

Single test: `PYTHONPATH=src .venv/bin/pytest tests/test_board.py -k backlog`.

## Toolchain

Plain venv + pip. **No uv anywhere** — it was removed deliberately after
`uv run` proved unreliable here; do not reintroduce it. No lockfile.

**Never use an editable install on this machine.** `pip install -e .` works
through a `.pth` in `.venv` that adds `src/`; it stops being honoured ~8
seconds after install, with the file present and readable, its target
existing, and `site` listing it in the directory. Reproduced identically with
pip and with uv, so it is not a packaging-tool problem. Root cause unknown.
Every make target therefore runs
`PYTHONPATH=src .venv/bin/python -m gitboard.cli`, which names `src` directly
and makes edits live with no reinstall. `make link` writes a
`~/.local/bin/gitboard` wrapper doing the same. **Do not "simplify" the
PYTHONPATH away.**

## Architecture

`src/` layout, package `gitboard`, entry point `gitboard.cli:app` declared in
`pyproject.toml`. `scripts/seed.py` and
`scripts/bulk_demo.py` are plain python3 (stdlib only) and shell out to
`.venv/bin/python -m gitboard.cli`.

- **`config.py`** — config singleton (`get_config()`, an `lru_cache(1)`).
  `configure()` applies CLI overrides and busts the cache. Token resolution is
  **lazy** (`Config.token()`), so `--help` never touches the keychain, and
  cached, so it resolves once.
  Precedence: **flag > env > `.env` > `gitboard.toml` > default**. Both
  `.env` and `gitboard.toml` are found by **walking up from the cwd**; when
  only `.env` did, running from `boards/` silently lost the repo config. A
  relative `spec` resolves against *the config file's* directory, not the cwd.
  Keys: `url`, `project`, `board`, `spec` — `project`/`spec` make the CLI
  arguments optional. A `token` key is deliberately **ignored with a warning**:
  credentials belong in `.env` or the keychain, not a file meant to be shared.
- **`log.py`** — console + logger singletons. **`out()` is stdout, `err()` is
  stderr.** The board goes to stdout; logs, spinners and change tables go to
  stderr, so `show -m | less` stays parseable. Nothing else may write stdout.
- **`client.py`** — the connection, and the only place API errors become
  English. Everything user-actionable raises `GitlabProblem`; the CLI prints it
  as one line and exits 1. No tracebacks for a typo'd path.
  `write_errors()` turns a 401/403 during `apply` into a message naming the
  scope, since a `read_api` token reads fine and fails only there.
- **`board.py`** — reading. `board_columns()` is why this repo exists: **no MCP
  server exposes board structure.** A board list is bound to a label and
  membership is "has that label", so the mapping is reassembled from
  `board.lists` + `project.issues`.
- **`report.py`** — reads `snapshots.jsonl`, no network: batches -> first/last
  diff -> per-assignee tally; `commit_counts` shells to `git log` and
  `match_author` joins heuristically (name or email local part).
- **`apply.py`** — the only writer (`apply`, `migrate_comments`, `close_issue`), and the
  spec schema's home: `spec_from_board`/`dump` are `pull`'s read direction,
  built so pull-then-plan is always empty. Uses `Config.token(write=True)`: the
  separate `GITLAB_WRITE_TOKEN` / `--write-token` / `gitlab-write-token`
  keychain slot, falling back to the read token when unset (a single `api`
  token is a valid setup). Two slots exist so a `read_api` token can be the
  only one the AI pass can reach.

### Testing notes

Tests never hit the network — the API surface is faked. The autouse fixture in
`tests/test_config.py` isolates `XDG_CONFIG_HOME` and chdirs somewhere empty,
and calls `config.reset()` on both sides. Without that, the singleton outlives
a test, the repo's own `.env` is found by the upward walk, and the developer's
user config leaks in — twelve tests failed before the fixture existed.

## Rendering rules

`board_columns` sorts by `_urgency` (overdue, then soonest due, then newest).
The API's order is not stable, and a truncated column has to show what the
reader would have gone looking for. `summarise` de-duplicates by iid — a
two-column issue is one issue, and summing per-column counts double-counts it.
`show` truncates to 5 per column by default; `--all` / `-n` override.
`tui` is a keypress loop over `board_view` (shared with `show`, so they
cannot drift) inside a rich `Live` alternate screen: reload / board-picker /
snapshot / edit (`$EDITOR` on the spec, pulled via `spec_from_board` if
missing, diff shown on return) / plan / apply-with-y/n /
migrate-with-close-y/n / help. The per-column truncation limit is computed
from terminal height each draw, and SIGWINCH redraws, so resizing works.
Raw input is `_key()` (termios cbreak, dies without a tty); all prompts
render inside the layout — `read_iid` echoes digits into the prompt line
and takes single-key escapes (b = pick a destination project in `m`).
Only `$EDITOR` stops the Live screen. Keybar labels are styled `Text`
chips, never markup — `[s]napshot` renders as strikethrough-then-text
because `[s]` is rich's strike tag.
`snapshot` appends `snapshot_records` (one JSON line per distinct issue) to a
JSONL file — the progress-over-time log.

Deliberate, not bugs: an issue with labels from two lists appears in both
columns (the web UI does the same, no tiebreak invented); `Backlog` is
synthesised for issues with no list label.

`as_markdown` is the stable rendering the `/board` prompt parses. Changing its
shape breaks that command — treat it as an interface.

## apply.py invariants

The YAML is the source of truth. Three normalisations exist because their
absence caused real bugs, each pinned by tests — **don't remove them**:

- Column `color` accepts friendly names (`COLORS` in apply.py); `load()`
  normalises to hex because the API only speaks hex, and `pull` maps known
  hexes back to names. Compare in hex or nothing is idempotent.
- An unquoted `2026-09-01` is a `datetime.date` to PyYAML, which `requests`
  cannot JSON-encode. `wanted_issue` coerces to ISO.
- GitLab strips a description's trailing newline; YAML's `|` keeps it, so
  every apply reported a phantom `description` change. `norm_text` fixes both
  sides — `wanted_issue` and `current_issue` must always agree, or nothing is
  idempotent.

Issue identity is the **title**. Renaming a title creates a second issue.
Apply is additive: nothing is deleted or closed, so removing an issue from the
YAML leaves it on the board. Closing exists but only as an explicit act —
`migrate-comments --close-source` / `close_issue()` — never as a side effect
of `apply`. `migrate_comments` skips system notes and the `superseded by`
breadcrumb `close_issue` leaves, or re-runs would copy the bookkeeping.

`ensure_project` resolves a namespace via `gl.namespaces.get`, not
`gl.user.username` — `gl.user` is `None` until `gl.auth()` runs.

`scripts/seed.py` owns only the token mint; the demo board is
`boards/demo.yaml` applied through the CLI. Re-running it reverts manual board
edits back to the YAML. `scripts/bulk_demo.py` generates `boards/demo-*.yaml`
(gitignored — the script is the source) and is idempotent per `--seed`.

## The AI pass writes now

`.claude/commands/board.md` may run `show`, `plan`, `report`, `apply`, and
edit `boards/*.yaml`. The contract is the flow, stated in the command: YAML
edit -> `plan` -> user go-ahead in conversation -> `apply --yes`. `apply` is
additive-only (nothing deleted or closed), which bounds the blast radius;
`migrate-comments` and its `--close-source` are deliberately NOT in the
command's allowed-tools — the AI suggests those lines, the user runs them.
The write token is `GITLAB_WRITE_TOKEN` in `.env`; pulling it (or
re-restricting `allowed-tools` to `show`/`plan`/`report`) revokes write
access — change both to go back to the old read-only guarantee.
`snapshot` and `report` touch only the local JSONL log, never GitLab.

Auth (read): `--read-token`, else `GITLAB_READ_TOKEN`, else keychain
`gitlab-read-token`. `GITLAB_TOKEN` and the `gitlab-token` keychain item are
honoured as pre-rename fallbacks; the env var warns.

Auth (write): `--write-token`, else `GITLAB_WRITE_TOKEN`, else keychain
`gitlab-write-token`, else the read token.

`GITLAB_URL` defaults to gitlab.com. `gitboard config` shows what resolved and
from where.
