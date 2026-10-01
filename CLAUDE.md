# CLAUDE.md

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
`estimate SPEC [--history h.json]` stages due dates from history (local only);
`status` (every local board: pulled ago, staged, notes, oldest in Verify,
overdue); `land SPEC` (plan, y/n, apply, snapshot, rotate `.base`);
`--all` on `pull`/`snapshot`/`report`; `report --since SPEC`; `stats`
(team markdown: open by column/epic/story, done, cycle time, verify
queue/times/coverage, stuck, questions, blocker flags; `--dump`/`--from`
for offline) and `digest` (writes `reports/<date>/<board>/{team,<user>}.md`
+ `.eml` for users named under `emails:`, and `graph.html` when the board
has blockers or milestones, and `gantt.html` (each mail also carries its
Gantt charts: own, current milestone, project); `--all`; Monday 07:00 in `make cron`);
`graph [PROJECT] [--from FILE] [-M MILESTONE] [--html PATH] [--mermaid]`
(which card waits on which, one tree per milestone). Every
`stats`/`digest` run appends one row per board to `reports/stats.jsonl`
(deduped per week); `stats --weeks N` (plus a per-milestone open (+added) table) and the mails' "8-week trend" read it.
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
pip and with uv, so it is not a packaging-tool problem. Likely cause (found in
Budgie, same machine): `site` silently skips a `.pth` carrying the macOS
`hidden` flag, and something here sets that flag inside the tree; `.venv`
itself carries it (`/bin/ls -lOd .venv`). Check the `.pth` with `/bin/ls -lO`
on site-packages; `chflags nohidden <file>` clears it. Budgie avoids it with a
venv outside the repo. Every make target therefore runs
`PYTHONPATH=src .venv/bin/python -m gitboard.cli`, which names `src` directly
and makes edits live with no reinstall. `make link` writes a
`~/.local/bin/gitboard` wrapper doing the same. **Do not "simplify" the
PYTHONPATH away.**

## Architecture

`src/` layout, package `gitboard`, entry point `gitboard.cli:app` declared in
`pyproject.toml`. `scripts/seed.py` and
`scripts/bulk_demo.py` are plain python3 (stdlib only) and shell out to
`.venv/bin/python -m gitboard.cli`.

Per-module notes, rendering rules and the `apply.py` invariants load on demand from `src/gitboard/CLAUDE.md`.

### Testing notes

Tests never hit the network — the API surface is faked. The autouse fixture in
`tests/test_config.py` isolates `XDG_CONFIG_HOME` and chdirs somewhere empty,
and calls `config.reset()` on both sides. Without that, the singleton outlives
a test, the repo's own `.env` is found by the upward walk, and the developer's
user config leaks in — twelve tests failed before the fixture existed.

## What the AI pass may write

`.claude/commands/board.md` may run `show`, `plan`, `report`, `apply`,
`ingest`, `estimate`, `status`, `stats`, `graph`, and edit `boards/*.yaml` (`land` is deliberately not
allowed). Staged `notes:` widen what `apply` can write to comments — still
additive, posted under a `*staged via gitboard*` first line, still shown in
the plan table first. The agent may move an issue **into** Verify, never
out: Done/Failed are people's verdict comments. It may stage
`milestone`, `milestones:`, `priority::N`, `blocked_by` additions and
removals and YAML reorders, proposing one fix per graph flag; it never
edits a `Blocked by:` footer. The contract is the flow, stated in the command: YAML
edit -> `plan` -> user go-ahead in conversation -> `apply --yes`. `apply` is
additive-only (nothing deleted or closed), which bounds the blast radius;
`migrate` (board reformats: `migrate.py`, ops in `boards/*.migration.yaml`,
one-way rows marked `!`, idempotent, `Verify`/`Done`/`Failed` refused as
targets; `/migrate-board` drafts the file, a person runs it) and
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
