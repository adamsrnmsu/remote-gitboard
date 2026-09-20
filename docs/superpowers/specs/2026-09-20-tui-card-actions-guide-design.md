# TUI card actions and guide mode — design

Beads: gb-vf2 (card actions), gb-04m (guide). Approved in conversation 2026-09-20.

## Why

`e` opens the whole board YAML in `$EDITOR`; moving one card means finding
it, swapping a label string, saving. That is no faster than opening the file,
so the TUI adds nothing to the one thing a lead does most. The YAML stays —
it is the agent's write path, the offline flow and what `plan` diffs — but a
person should not have to hand-edit it. And a new user has no idea what `m`
or `a` will do, or what to type, until they have read the docs.

## Card actions (gb-vf2)

Keys stage a change into the YAML; `p` / `a` are unchanged. `e` stays for
bulk edits.

| key | action | flow |
|---|---|---|
| `v` | move | card number, then a column by number (Backlog = no column label) |
| `u` | assign | card number, then a person by number, or `t` to type a username |
| `d` | due | card number, then `YYYY-MM-DD`, `+N` days, or `e` for the estimate |
| `c` | comment | card number, then one line; staged under `notes:` |
| `n` | new card | a title, then a column |

- `edit.py`, pure: `find`, `adopt`, `move`, `assign`, `set_due`, `add_note`,
  `new_card`. Each mutates the spec dict and returns the staged line
  (`#12 Doing → Review`); a refusal raises `EditError` with the reason.
- **Verdict rule holds for people too**: a card in `Verify` is not moved by
  key, and `Done` / `Failed` are not targets — those are a `verified:` /
  `failed:` comment on the card. Moving *into* Verify is fine.
- A card on the board but missing from a stale YAML is adopted first:
  `apply.issue_entry(issue)` (factored out of `spec_from_board`) builds the
  same entry `pull` would.
- No YAML yet: the first action pulls one, as `e` does.
- After each action: status `staged: … · p shows, a applies`, and a
  "staged this session" panel. No network call per action.
- `d` then `e` uses `estimate.estimate` over a history fetched once per
  session; offline it says there is no history here.
- Every write is `apply.load` → mutate → `apply.dump`, the path `ingest`
  already uses (same ceiling: YAML comments are dropped).

Cursor (gb-j76, added the same day): arrows or `h j k l` select a card and
the card keys act on it with no number to type; `esc` drops it. The cursor is
a position `(column, row among the shown cards)`, drawn by
`board_view(selected=...)`, and follows its card across a reload. An offline
`(new)` card has no number, so `edit.find` also takes a title.

Limit, deliberate: a due date cannot be cleared by key.

## Guide mode (gb-04m)

Pressing a mode key shows a panel beside the prompt: what it does, a worked
example, what it will and will not write. It never blocks and costs no
keypress, which is why it can be on by default.

- `guide.py`: `KEYS_ONLINE`, `KEYS_OFFLINE` (the keybar's pairs, moved out of
  the closure) and `GUIDE: {key: (title, [lines])}`. A test pins that every
  keybar key has an entry, so a new key cannot ship without one.
- Off: `guide = false` in `gitboard.toml`, `GITBOARD_GUIDE=0` (env or
  `.env`), or `tui --no-guide`. `g` toggles it for the session. Precedence is
  the config's usual flag > env > `.env` > toml > default (`true`).
- Panels for keys that act at once (`r`, `s`, `p`) show after the action, with
  the result; keys that prompt (`v u d c n m a b e`) show it with the prompt.

## Tests

`tests/test_edit.py`: each action's happy path and staged line; Verify /
Done / Failed refusals; adopt; duplicate title; `+N` and bad dates; note
de-duplication. `tests/test_guide.py`: keybar coverage; `tests/test_config.py`:
`guide` default, toml, env strings. The keypress loop itself stays untested
(raw tty), so it only glues prompts to `edit.py`.
