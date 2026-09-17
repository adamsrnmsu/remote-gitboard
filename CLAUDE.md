# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Goal — judge every change against this

A task-tracking experience for the team that **offloads cognitive burden from
the lead**, **keeps up with the pace of AI-driven work**, and **builds in the
verification that pace demands**. The board must stay truthful without the
lead curating it by hand; agents do the reading, sorting, drafting and
staging; the human does review and decisions. Verification is a first-class
state, not an afterthought: work an agent produced is not done until a person
has checked it and the board shows that. If a feature does not reduce what
the lead has to hold in their head, or does not make the board more truthful
or verification cheaper, it does not belong here.

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
destinations are iids or `group/project#iid`; `pull --base` (also writes an
untouched `<file>.base`), `--notes` (pull comments as `discussion:`),
`--force` (overwrite); `show --from FILE`, `plan FILE --against BASE`,
`tui --from FILE` — the no-network trio for a container: the pulled YAML is
the board, the agent edits it, the host runs `plan` then `apply`;
`ingest TASKS.md --into SPEC` folds a tasks.md into the YAML (local only);
`status` (every local board: pulled ago, staged, notes, oldest in Verify,
overdue); `land SPEC` (plan, y/n, apply, snapshot, rotate `.base`);
`--all` on `pull`/`snapshot`/`report`; `report --since SPEC`; `stats`
(team markdown: open by column/epic/story, done, cycle time, verify
queue/times/coverage, stuck, questions; `--dump`/`--from` for offline) and
`digest` (writes `reports/<date>/<board>/{team,<user>}.md` + `.eml` for
users named under `emails:`; `--all`; Monday 07:00 in `make cron`).
Docs: `make docs` (Sphinx, `docs/`). See README "Offline".

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
- **`stats.py`** — pure, stdlib: `summarise(history, ...)` over the dicts
  `board.fetch_history` returns (issues incl. recently closed, label
  transitions from `resource_label_events`, verdict and question notes);
  `for_person`, markdown renderers, `eml`. "Done" is the Done column or a
  close (`done_at`). Scoped labels `epic::`/`story::`/`type::` are the
  grouping vocabulary; they stay plain labels in the YAML. `emails:` in the
  spec maps username to address for `digest`.
- **`mail.py`** — the HTML digest, stdlib only. Outlook on Windows renders
  with Word, so: 600px tables, inline styles, px widths, no images, no SVG,
  every `td` with `bgcolor` and every text run with a `color` (that is what
  survives Outlook's dark-mode invert; a test enforces it). Charts are table
  cells (`column_chart` burndown, `bar_row`); the browser copy adds one
  inline SVG line. Palette validated with the dataviz skill's script against
  `#14171c`; column colours are fixed in `COLUMN_COLORS`. Each mail opens
  with "Your 3 moves" (`stats.three_moves`) and impact tiles. `.eml` is
  multipart/alternative (markdown text + HTML); `digest` also writes
  `index.html` for browser previews.
- **`ingest.py`** — pure: `parse` a tasks.md (`docs/tasks-md-contract.md`:
  header `commit:`/`mr:`, `## Person`, `- [ ] title · id: T-slug`, verify
  lines, optional `**Feedback**`) and `merge` it into a spec. New tasks
  become issues in `Verify` with the verify steps as a `- [ ]` task list and
  a `Source:` footer (source · person · date · id · commit). **Done and
  Failed come only from a person's `verified:` / `failed:` comment**
  (`verdict` scans `discussion:`); a `[x]` in the file is advisory and
  reported, never acted on. Footer `id:` then normalised title is identity;
  a near-duplicate title (difflib ≥ 0.85) is reported, not added. `stale`
  (task vanished from its file) and `re-verify` (footer commit changed) are
  labels, never moves. `merge` reports `changed`; the CLI writes only then.
- **`apply.py`** — the only writer (`apply`, `migrate_comments`, `close_issue`), and the
  spec schema's home: `spec_from_board`/`dump` are `pull`'s read direction,
  built so pull-then-plan is always empty. `diff(spec, have, base=None)` is
  the pure core; `plan` builds `have` from the API (`state="all"`),
  `have_from_spec` from a pulled YAML (offline). `issue_changes` is the one
  three-way decision point both plan and apply use, so they cannot disagree:
  with `base` (the `.base` copy), a field the YAML did not change but GitLab
  did is `skipped` (kept), a field both changed is `drift` (refused unless
  `force`). Change kinds: added `+`, changed `~`, skipped `-`, drift `!`;
  details say old -> new. Assignees compare by username on both sides; ids
  are resolved by `resolve_users` before any write, never inside the diff. Uses `Config.token(write=True)`: the
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
shape breaks that command — treat it as an interface. `columns_from_spec`
builds the same column shape from a YAML so every renderer works offline;
entries with no `iid` yet print as `(new)`.

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

Issue identity is the **iid when the entry has one, else the stripped
title**: a retitle in a pulled YAML is a rename; on a hand-written entry it
creates a second issue. `load()` strips titles and rejects duplicates; two
open GitLab issues sharing a title is an error, not a coin toss. A title
whose only live match is **closed** is skipped, never recreated. Apply is
additive: nothing is deleted or closed, removing an issue from the YAML
leaves it on the board, and **labels the YAML does not name survive** (only
column labels and labels the spec mentions are managed). Per-issue `notes:` are staged comments: `apply`
posts each body not already on the issue (`ensure_notes`, same idempotency
rule as `migrate_comments`); `discussion:` is what `pull --notes` read and is
never written. `plan`/`diff` report notes as `("added", "note", ...)`; the
online `plan` fetches notes only for issues that stage some. `people:`,
`iid`, `discussion:` are spec keys apply ignores. Closing exists but only as an explicit act —
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

`.claude/commands/board.md` may run `show`, `plan`, `report`, `apply`,
`ingest`, `status`, and edit `boards/*.yaml` (`land` is deliberately not
allowed). Staged `notes:` widen what `apply` can write to comments — still
additive, posted under a `*staged via gitboard*` first line, still shown in
the plan table first. The agent may move an issue **into** Verify, never
out: Done/Failed are people's verdict comments. The contract is the flow, stated in the command: YAML
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

## Tasks: beads

Task tracking is `bd` (beads); `.beads/` is the Dolt store, `AGENTS.md` the
pointer. `bd ready` is the queue, `bd update <id> --claim` to start,
`bd close <id> --reason "..."` to finish; follow-ups become beads, not TODOs.
Sync is `bd dolt push/pull` on the git remote, never a committed JSONL.
