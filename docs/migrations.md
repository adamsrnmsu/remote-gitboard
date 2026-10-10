# Migrations: reformatting a board

`push` does not rename a label everywhere, merge two labels or move a card to
another project (it does close an issue marked `closed: true` and drop a
column removed from `columns:`, keeping its label). A reformat needs exactly those one-way moves, so
they live in a separate file and a separate command, and the agent never
runs it.

## The pattern

1. `/migrate-board group/project` — the agent pulls the board, reads the
   stats and the movement log, and writes two files:
   `boards/<name>.migration.yaml` (the ops) and `boards/<name>.yaml` (the
   board as it should look afterwards). It shows a proposal table and the
   `plan` table and stops.
2. You read the tables and run:

```bash
gitboard migrate boards/<name>.migration.yaml   # ! rows are one-way; y/n
gitboard push boards/<name>.yaml               # the rest
gitboard pull group/project --force -o boards/<name>.yaml
```

Every op is idempotent: running the file twice prints only `-` skipped rows.
A failed op leaves the earlier ones applied; fix and rerun.

## The file

```yaml
project: grp/proj
board: Dev Board
ops:
  - rename_label: {from: bug, to: type::bug}
  - order_columns: [Backlog, Doing, Review, Verify, Done]
  # 14 cards carry p1 or urgent; both mean the same thing
  - merge_labels: {from: [p1, urgent], to: priority::high}
  - drop_column: {name: Blocked, keep_label: true}
  - move_issues: {label: epic::Billing, to: grp/billing}
  - split_board: {name: Billing board, columns: [Doing, Verify, Done]}
```

| op | does | reversible | trace on GitLab |
|---|---|---|---|
| `rename_label` | renames the label in place; every card keeps it, history intact | yes | GitLab's own label event |
| `order_columns` | reorders the board lists; unnamed lists follow in their current order | yes | none needed |
| `split_board` | creates a board with those columns on the same project | yes | none needed |
| `merge_labels` | relabels every card carrying any `from` to `to`, then deletes the `from` labels | **no** | one note per card: `*migrated by gitboard: p1, urgent -> priority::high*` |
| `drop_column` | removes the list; with `keep_label: false` also strips and deletes the label | **no** | one note per stripped card |
| `move_issues` | moves cards (by label or `iids`) to another project; target labels are created first so they survive the move | **no** | GitLab's "moved to" note on the source, which is closed |

`Verify`, `Done` and `Failed` are fixed names: verdicts, `stats` and the
8-week trend depend on them. `migrate` refuses to rename, merge or drop
them; migrate the other labels around them.

## Labels in the board YAML

Non-column labels get colours and descriptions through a top-level
`labels:` list; `push` creates and fixes them, `pull` writes them back, so
scoped labels stop getting random colours:

```yaml
labels:
  - {name: "epic::Payments", color: gitlab blue, description: Checkout and billing}
  - {name: "type::bug", color: crimson}
```
