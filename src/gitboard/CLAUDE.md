# src/gitboard — module notes

Moved from the root `CLAUDE.md`. Read before changing a module here.

## Modules

- **`config.py`** — config singleton (`get_config()`, an `lru_cache(1)`).
  `configure()` applies CLI overrides and busts the cache. Token resolution is
  **lazy** (`Config.token()`), so `--help` never touches the keychain, and
  cached, so it resolves once.
  Precedence: **flag > env > `.env` > `gitboard.toml` > default**. Both
  `.env` and `gitboard.toml` are found by **walking up from the cwd**; when
  only `.env` did, running from `boards/` silently lost the repo config. A
  relative `spec` resolves against *the config file's* directory, not the cwd.
  Keys: `url`, `project`, `board`, `spec`, `guide` — `project`/`spec` make
  the CLI arguments optional; `guide` (default true, `GITBOARD_GUIDE`) is the
  TUI's per-mode guide. A `token` key is deliberately **ignored with a warning**:
  credentials belong in `.env` or the keychain, not a file meant to be shared.
- **`log.py`** — console + logger singletons. **`out()` is stdout, `err()` is
  stderr.** The board goes to stdout; logs, spinners and change tables go to
  stderr, so `show -m | less` stays parseable. Nothing else may write stdout.
- **`client.py`** — the connection, and the only place API errors become
  English. Everything user-actionable raises `GitlabProblem`; the CLI prints it
  as one line and exits 1. No tracebacks for a typo'd path.
  `write_errors()` turns a 401/403 during `push` into a message naming the
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
  `for_person`, markdown renderers, `eml`. `by_milestone` (open/done/added per
  milestone by `created_at`, due date) is top-level in the summary and a
  `stats.jsonl` field. "Done" is the Done column or a
  close (`done_at`). Scoped labels `epic::`/`story::`/`type::` are the
  grouping vocabulary; they stay plain labels in the YAML.
  `weak_verdicts` flags a latest `verified` whose task list
  (`task_completion_status`, kept as `tasks: [ticked, total]`) is not fully
  ticked, or that came within `FAST_VERIFY` of entering Verify — a flag in
  stats/digest (`verify.weak`, `weak` in `stats.jsonl`), never a move. `emails:` in the
  spec maps username to address for `digest`.
  Team report is built once as blocks (`team_blocks`);
  `render_team_md = to_md(team_blocks(...))`, byte-identical (golden test
  `tests/golden_team_md.json`). `PI_BLOCKS=1` makes `stats` emit JSON-lines
  blocks for perch tui; contract at
  `perch/docs/superpowers/specs/2026-10-02-tui-blocks-design.md`.
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
- **`estimate.py`** — pure, stdlib: a sample is a finished card's active days
  (first column `add` -> `done_at`); `estimate` takes the nearest-rank
  `median`/`p85` of the narrowest bucket with `min_samples` — person+`type::`,
  person, team+`type::`, team — else None, never a guess. `suggest` stages
  `due_date = today + days` on assigned, undated cards outside
  Verify/Done/Failed and **never overwrites a date**; `estimates.suggest_due:
  false` makes it print only. `tight` (due before the expected finish) is
  attached by `cli._summary` as `flow.tight` — `estimate` imports `stats`,
  so **`stats` must not import it back**; renderers read it with `.get`.
  `late_milestones` (a milestone's critical chain, cards in sequence, summed
  against its due date; a card without an estimate makes it a lower bound) is
  attached the same way as `flow.late_milestones`.
  `_history` fetches back at least `HISTORY_DAYS` (90) so a weekly run has
  samples.
- **`links.py`** — blocker refs: `"9"` (this project), `"grp/x#4"`
  (another), `"new:<title>"` (a spec card with no iid yet). `norm_refs`
  turns YAML entries (int, `group/project#iid`, exact title) into refs;
  `check` is `load()`'s cycle/self-block/unknown-title guard (stdlib
  `graphlib`); `priority` is the lowest `priority::N` digit. `read`/`sync`
  are the only API-touching parts: native `is_blocked_by` links, plus a
  `Blocked by: #9, grp/x#4` last description line that is **read, never
  written** — it keeps the CE demo drawable, since CE silently stores
  `blocks` as `relates_to`. `sync` re-reads after creating and, on that
  downgrade, deletes the link and raises one `GitlabProblem`. A footer ref
  is never duplicated as a link nor removed (skipped: edit the
  description). Must not import `apply`, `board` or `graph`.
- **`order.py`** — pure: the board order (`relative_position`, one per
  project) as one three-way field. `merge(edited, live, old)` compares only
  iids on every side; no `old` (no `.base`) returns None, so a hand-written
  YAML never reshuffles the board. `moves` keeps the longest run already in
  order (patience LIS, `bisect`) and moves every other card once, since
  GitLab reorders one card per call.
- **`graph.py`** — pure, stdlib (plus rich for the tree): `build(cards, milestones)`
  (known milestones seed a card-less root, "no cards yet")
  over `fetch_history`'s card shape (or `cards_from_spec` from a YAML, where
  a same-project blocker missing from the pull counts as closed) gives
  nodes, edges (blocker -> card, card -> `m:<milestone>`), `downstream`
  counts, the `critical` longest chain per milestone (ties: lowest ref) and
  Sugiyama-lite `layers`. `subgraph` keeps one milestone and its upstream.
  `flags(cards, columns)` is the six contradiction lists stats and digest
  carry — flags, never moves; the `blocked_*` two need a Blocked column;
  `no_milestone` fires only once some open card has a milestone (Verify,
  Failed and Done cards excluded) and the graph's ⚑ skips it.
  `render_tree(g, today, flagged, late=None)` (one rich tree per milestone, root shows "forecast N days late" from the caller's `late` {title: days}, a shared blocker printed once,
  then `(see #9 above)`) and `render_mermaid` (escaped labels, `i12` /
  `m_<slug>` ids). Must not import `estimate`, `stats`, `apply`, `board` or `cli`.
- **`graph_html.py`** — `render_html(g, title, flagged)`: one
  self-contained page, inline SVG from `layers`, a native `<title>` per
  node for hover, ~30 lines of JS for click-to-highlight up- and
  downstream. No CDN, no vendored library: it works air-gapped and as a
  digest file. Every title goes through `html.escape`; the adjacency JSON
  escapes `<`, so a `</script>` title cannot close the script.
- **`gantt.py`** — the digest's Gantt charts, hacker-dark (`HACK`,
  `STATUS`; palette run through the dataviz validator against `#0a0f0a`:
  CVD at the 6.1 floor, so every row also prints its status word). Same
  Outlook rules as `mail.py`, whose `td` it uses: a track is painted per
  pixel then run-length encoded into `td`s (`paint`), so clipping (`◂`),
  the 4px minimum and the today/due markers need no special cases. `bars`:
  start = `estimate.started` (Backlog: today), end = done, else due (past
  due: `late`, ends today), else `estimate._expected` (`est`), else today
  (`undated`); a Backlog card with neither is left out, done cards older
  than `LOOKBACK` too. `current_milestone`: earliest due with open cards,
  else most open cards. `groups` is the project view (per milestone, else
  per `epic::`). `person_blocks` / `team_blocks` feed the mails' TIMELINE
  zone (`mail._timeline`), capped at `ROWS`; `gantt.html` is `team_blocks`
  uncapped. Imports `mail`; `mail` must not import it back.
- **`edit.py`** — pure: the spec mutations behind the TUI's card keys
  (`move`, `assign`, `set_due`, `add_note`, `new_card`, `adopt`). Each
  returns the staged line or raises `EditError`. **The verdict rule holds
  for people too**: no move out of Verify, Done/Failed are never targets.
- **`guide.py`** — the TUI keybar (`rows`) and the guide texts (`GUIDE`); a
  test pins that every keybar key has a text and every prompting key an
  example, so a new key cannot ship unexplained.
- **`migrate.py`** — the one-way writer, kept out of `apply` and out of the
  agent's tools: `rename_label`, `order_columns`, `split_board`
  (reversible) and `merge_labels`, `drop_column`, `move_issues` (one-way,
  `!` rows with card counts). Every op is idempotent; a rerun prints only
  `skipped`. Merges and drops leave `*migrated by gitboard: …*` on each
  touched card, which is also the idempotency marker. Label rename is
  `new_name` (in place, cards keep it); `move_issues` creates the target
  labels first because GitLab drops labels the target lacks.
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
- **`apply.py`** — the only writer (the `push` command; module name kept) (`apply`, `migrate_comments`, `close_issue`), and the
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

## Rendering rules

`board_columns` sorts by `_urgency`: overdue first, then GitLab's board
order (`relative_position`, nulls last, then newest). A truncated column
has to show what the reader would have gone looking for, and otherwise
the order the team sees in GitLab — the one the lead and Claude set
through the YAML. `columns_from_spec` uses the YAML's list order. `summarise` de-duplicates by iid — a
two-column issue is one issue, and summing per-column counts double-counts it.
`show` truncates to 5 per column by default; `--all` / `-n` override.
`tui` is a keypress loop over `board_view` (shared with `show`, so they
cannot drift) inside a rich `Live` alternate screen: reload / board-picker /
snapshot / edit (`$EDITOR` on the spec, pulled via `spec_from_board` if
missing, diff shown on return) / plan / push-with-y/n /
sync (push, snapshot, refresh the YAML: `_refresh_spec`, shared with the
`sync` command) / pull (`_pull_board`, shared with `pull`; warns and asks y/n
before overwriting, listing `_staged_edits`; online only) /
migrate-with-close-y/n / help. The per-column truncation limit is computed
from terminal height each draw, and SIGWINCH redraws, so resizing works.
Raw input is `_key(timeout=None)` (termios cbreak, dies without a tty; with a timeout it `select`s the fd and returns None, which the main loop treats as an auto-reload tick: `--watch MINUTES`, or offline a 2 s mtime poll; prompts call `_key()` with no timeout so they block and never reload; reload only reads). `board.moved` diffs columns on every refetch and `board_view(marked=)` draws `●` before those cards until the next reload (first load marks nothing); all prompts
render inside the layout — `read_iid` echoes digits into the prompt line
and takes single-key escapes (b = pick a destination project in `m`).
Card keys `v u d c n` stage into the YAML through `stage()` -> `edit.py`:
the file is read **raw** (`raw_spec`; `load` only validates, because it
rewrites colour names to hex), a card a stale YAML lacks is adopted via
`apply.issue_entry`, and nothing touches GitLab until `a` (push). The guide panel
(`st["tip"]`) is set per key and never blocks. The cursor (`st["cursor"]`,
`(column index, row among the shown cards)`; arrows or `hjkl`, `esc` drops
it) is a **position, not an iid** — a two-column card is on screen twice and
an offline `(new)` card has no number, so `edit.find` takes an iid or a
title. `board_view(selected=...)` draws it; `_move_cursor` and `_find_card`
(the selection follows its card across a reload) are pure and tested.
`_key()` reads the fd, not `sys.stdin`: an arrow is three bytes in one read,
and a paste is many keys, queued in `_pending` by `_split_keys`. The keybar is two rows
(board, card); the status line is its own line above it.
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
  every push reported a phantom `description` change. `norm_text` fixes both
  sides — `wanted_issue` and `current_issue` must always agree, or nothing is
  idempotent.

Issue identity is the **iid when the entry has one, else the stripped
title**: a retitle in a pulled YAML is a rename; on a hand-written entry it
creates a second issue. `load()` strips titles and rejects duplicates; two
open GitLab issues sharing a title is an error, not a coin toss. A title
whose only live match is **closed** is skipped, never recreated. Apply is
additive for issues and milestones: nothing is deleted or closed, removing an issue from the YAML
leaves it on the board, and **push only manages column labels and labels the spec uses; any other label a card has in GitLab stays on it**. Per-issue `notes:` are staged comments: `push`
posts each body not already on the issue (`ensure_notes`, same idempotency
rule as `migrate_comments`); `discussion:` is what `pull --notes` read and is
never written. `plan`/`diff` report notes as `("added", "note", ...)`; the
online `plan` fetches notes only for issues that stage some. `people:`,
`iid`, `discussion:` are spec keys push ignores.

Blockers, milestones and order: **an absent key is unmanaged.** No
`blocked_by` key leaves a card's links alone (`[]` removes them); no
`milestone` key leaves its milestone alone (`null` clears it); without
a `.base` the order is never touched — otherwise a hand-written spec
would wipe every link and reshuffle the board. Removing a **link** is
allowed, like removing a label: it is reversible metadata. `milestone`
and `blocked_by` are two more fields of `issue_changes`, and the order
is one more three-way field (`order_changes` over `order.merge`), so
plan and apply still share one decision point. `ensure_milestones`
creates and updates, never closes; titles resolve to ids before any
write, like users. `pull` writes issues in board order and a
`milestones:` entry for every milestone a card carries plus every active
project milestone (card-less group ones stay out, gb-84c), so
pull-then-plan stays empty. The plan table lists notes, then link and
order rows and `blocked_by` changes, then the rest (`cli._review_first`). Closing exists but only as an explicit act —
`migrate-comments --close-source` / `close_issue()` — never as a side effect
of `apply`. `migrate_comments` skips system notes and the `superseded by`
breadcrumb `close_issue` leaves, or re-runs would copy the bookkeeping.

`ensure_project` resolves a namespace via `gl.namespaces.get`, not
`gl.user.username` — `gl.user` is `None` until `gl.auth()` runs.

`scripts/seed.py` owns only the token mint; the demo board is
`boards/demo.yaml` applied through the CLI. Re-running it reverts manual board
edits back to the YAML. `scripts/bulk_demo.py` generates `boards/demo-*.yaml`
(gitignored — the script is the source) and is idempotent per `--seed`.
