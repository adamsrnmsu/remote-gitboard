# TUI card actions and guide mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Card-level keys in the TUI that stage changes into the board YAML, and an on-by-default guide panel per mode.

**Architecture:** Pure `edit.py` owns every spec mutation and refusal; pure `guide.py` owns the keybar pairs and guide texts; the `tui` closure in `cli.py` only prompts, calls them, and writes the YAML. `config.py` gains one key.

**Tech Stack:** Python stdlib, rich, typer, pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-tui-card-actions-guide-design.md`

## Global Constraints

- `PYTHONPATH=src .venv/bin/...`; no uv, no editable install; tests never touch the network.
- Keybar labels are styled `Text`, never markup (`[s]` is rich's strike tag).
- All prompts render inside the Live layout; only `$EDITOR` stops it.
- A card in `Verify` is never moved by key; `Done`/`Failed` are never targets.
- YAML writes go `apply.load` → mutate → `apply.dump`.
- Beads: claim on start, close with a reason. Commit only when asked.

---

### Task 1 (gb-vf2.1): `edit.py` + `apply.issue_entry`

**Files:** Create `src/gitboard/edit.py`, `tests/test_edit.py`; modify `src/gitboard/apply.py` (`spec_from_board`).

**Produces:**
- `apply.issue_entry(issue) -> dict` — title, iid, labels, description, due_date, assignee; empty fields dropped. `spec_from_board` uses it (then adds `discussion`).
- `edit.EditError(Exception)`; `edit.BACKLOG = "Backlog"`; `edit.LOCKED = {"Verify"}`; `edit.VERDICT_ONLY = {"Done", "Failed"}`
- `edit.columns(spec) -> list[str]`; `edit.column_of(spec, entry) -> str` ("+"-joined, else Backlog)
- `edit.find(spec, iid) -> dict | None`; `edit.adopt(spec, entry) -> dict` (returns the existing entry when the iid is already there)
- `edit.move(spec, iid, column) -> str`, `edit.assign(spec, iid, username) -> str`, `edit.set_due(spec, iid, text, today) -> str`, `edit.add_note(spec, iid, body) -> str`, `edit.new_card(spec, title, column) -> str`

- [ ] Tests first (`tests/test_edit.py`): move swaps only column labels and keeps the rest sorted-stable; move to Backlog drops the column label; move out of Verify, into Done, into Failed, to an unknown column, to the same column, and an unknown iid each raise `EditError` naming the reason; move into Verify works; assign sets and reports `old → new`; `set_due` takes ISO and `+3` (from `today`), rejects `tomorrow` and `2026-13-40`; `add_note` appends once and refuses an empty or duplicate body; `new_card` appends `{title, labels}`, refuses a blank or case-insensitively duplicate title and a verdict-only column; `adopt` appends once. `tests/test_apply.py`-side: pull-then-plan stays empty (existing tests cover it once `spec_from_board` uses `issue_entry`).
- [ ] Run → import error. Implement. Run → green, ruff clean.

### Task 2 (gb-vf2.2): TUI keys `v u d c n`

**Files:** Modify `src/gitboard/cli.py` (tui closure only).

- [ ] Generalise `read_iid` into `read_text(label, hint, extra="", digits=False)`; `read_iid` becomes a call with `digits=True` returning `int | str | None`.
- [ ] Generalise `pick_board`'s overlay into `pick(title, options, panel_title, extra="")` → index or the extra key or None; `pick_board` uses it.
- [ ] `stage(fn)`: ensure a spec file (pull when missing, as `e`), `apply.load`, adopt the live issue when `edit.find` misses, call `fn(spec)`, `apply.dump` to disk, append the line to `st["staged"]`, set status + the "staged this session" panel; `EditError` → status in the error style, nothing written. Offline: `refetch()` after, since the YAML is the board.
- [ ] Wire `v u d c n`; `d`+`e` → history cached in `st["history"]` via `_history(..., estimate_mod.HISTORY_DAYS // 2)`, `estimate_mod.estimate(...)`, basis shown in the status; offline → "no history here — type a date".
- [ ] `a` success clears `st["staged"]`. Update `help_panel` and the `tui` docstring/`--from` help.
- [ ] Verify by driving the TUI on the local GitLab through a pty (scripted keys), then `plan` shows the staged changes.

### Task 3 (gb-04m.1): guide mode

**Files:** Create `src/gitboard/guide.py`, `tests/test_guide.py`; modify `src/gitboard/config.py`, `tests/test_config.py`, `src/gitboard/cli.py`.

**Produces:** `guide.KEYS_ONLINE`, `guide.KEYS_SPEC` (p, a — only with a YAML), `guide.KEYS_OFFLINE`: `list[tuple[str, str]]`; `guide.GUIDE: dict[str, tuple[str, list[str]]]`; `guide.panel(key) -> rich Panel | None`; `Config.guide: bool`; `tui --no-guide`.

- [ ] Tests: every key in the three lists has a `GUIDE` entry with a title and ≥ 2 lines, one of which contains "Example"; `Config.guide` defaults True, `guide = false` in toml → False, `GITBOARD_GUIDE=0`/`false`/`off` → False, `1` → True.
- [ ] Implement; keybar reads the lists; `g` toggles `st["guide"]`; prompts show the panel via `st["extra"]` (kept while a prompt redraws).
- [ ] Verify through the pty drive: panel text appears on `m`, not with `--no-guide`.

### Task 4 (gb-vf2.3): docs

- [ ] `docs/commands.md` (TUI keys), `README` TUI section if it lists keys, `CLAUDE.md` Rendering rules paragraph (keys, `edit.py`, `guide.py`, `guide` config key), `docs/install.md` or wherever `gitboard.toml` keys are listed. `make docs` clean.
