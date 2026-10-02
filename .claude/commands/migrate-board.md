---
description: Propose a board reformat — draft the migration file and the new YAML; a person runs it.
argument-hint: <group/project> [board name]
allowed-tools: Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli show:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli pull:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli plan:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli stats:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli status:*), Bash(PYTHONPATH=src .venv/bin/python -m gitboard.cli report:*), Edit(boards/*.yaml), Edit(boards/*.migration.yaml)
---

`gitboard` below is `PYTHONPATH=src .venv/bin/python -m gitboard.cli`.

You draft a reformat. You never run it: `gitboard migrate` is not in your
tools, on purpose. Its ops are one-way (merged labels, dropped columns,
moved cards); a person reads your table and runs the line you leave.

**Read.** `gitboard pull $ARGUMENTS --base --force -o boards/<name>.yaml`
if the YAML is stale or missing (the `.base` is what `plan` diffs
against), then `gitboard show $ARGUMENTS --markdown`, `gitboard stats
$ARGUMENTS`, and `gitboard report $ARGUMENTS` when `snapshots.jsonl` exists.

**Look for** (each one becomes a row in your proposal, or is left alone):
- labels that mean the same thing (`bug`, `Bug`, `type:bug`) → `merge_labels`
- labels outside the vocabulary that should be scoped (`urgent` →
  `priority::high`, `payments` → `epic::Payments`) → `rename_label`
- columns nothing has moved through in the report window → `drop_column`
  (keep the label unless it is noise)
- a column order that does not read left-to-right as a flow → `order_columns`
- one board carrying two unrelated streams → `split_board`, or
  `move_issues` to the project that owns them
- cards with no `epic::` / `story::` / `type::` → not a migration; set them in
  the YAML like any other edit

**Fixed names.** `Verify`, `Done` and `Failed` are semantic: verdicts,
`stats` and the 8-week trend depend on them. Never rename, merge or drop
them; migrate the other labels around them. `migrate` refuses these anyway.

**Write two files.**
1. `boards/<name>.migration.yaml` — `project`, `board`, and an ordered
   `ops:` list. One op per line, reversible ones first
   (`rename_label`, `order_columns`, `split_board`), then one-way ones
   (`merge_labels`, `drop_column`, `move_issues`). A YAML comment above each
   one-way op says why and how many cards it touches.
2. `boards/<name>.yaml` — the board as it should look *after* the migration:
   new column order, `labels:` with colours and descriptions for every
   scoped label you introduce, cards carrying the new labels. Then run
   `gitboard plan boards/<name>.yaml` and show its table. Additive rows
   only; `plan` cannot see the migration, so rows that the migration will
   make redundant (an issue still carrying `bug`) are expected — say so.

**Report**, in this order and nothing else:

## Proposal
One table: `now → after · op · cards · reversible?`. One row per op.

## Plan table
The `gitboard plan` output for the new YAML, verbatim.

## Run it
```
gitboard migrate boards/<name>.migration.yaml   # shows ! rows first, asks y/n
gitboard push boards/<name>.yaml               # then the additive rest
gitboard pull $ARGUMENTS --base --force -o boards/<name>.yaml
```
Then stop. Do not edit `discussion:`, do not invent iids, do not retitle
cards without an iid. If the board needs no reformat, say that in one line
and write nothing.
