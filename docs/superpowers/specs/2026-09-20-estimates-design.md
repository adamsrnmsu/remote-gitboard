# Per-person estimates — design

Bead: gb-j38. Approved in conversation 2026-09-20.

## Why

Due dates on the board are guesses the lead makes by hand. The board already
records how long each person's cards of each type actually take. An estimate
drawn from that history right-sizes a due date without the lead holding
anyone's pace in their head, and a date that contradicts the history is worth
a warning before it becomes an overdue card.

## What

Two outputs from one estimator, and a knob:

- **A — suggest.** `gitboard estimate SPEC` writes `due_date` into the board
  YAML for cards that have none. Local file only; the usual
  `plan` → go-ahead → `apply` carries it to GitLab.
- **B — warn.** `stats` and `digest` list **tight dates**: cards whose
  `due_date` falls before the date the history says they will finish.
- **Knob.** `estimates.suggest_due: false` turns A off: `estimate` prints
  the same table and writes nothing. B is unaffected.

```yaml
estimates:            # optional; these are the defaults
  suggest_due: true
  method: p85         # or median
  min_samples: 5
```

An unknown `method`, a non-mapping `estimates:`, or `min_samples < 1` is a
`SpecError`.

## The estimator (`estimate.py`, pure, stdlib)

- **Sample**: one finished card (`stats.done_at` is set) →
  `(assignee, type, active_days)`. `active_days` runs from the card's first
  column-label `add` transition (falling back to `created_at`) to `done_at`.
  Backlog wait is not the person's speed.
- **Ladder**: the first bucket holding at least `min_samples` samples wins:
  person + `type::`, person, team + `type::`, team. Rungs that need a
  missing assignee or type are skipped. No bucket → no estimate. It never
  guesses from fewer.
- **Method**: nearest-rank percentile of the bucket (`median` = 0.5,
  `p85` = 0.85), so the figure is a duration that really happened. Rounded
  up to whole days, minimum 1.
- **Result**: `{"days": 4, "n": 9, "basis": "p85 of 9 cards: alice, type::bug"}`.

## A — `gitboard estimate [SPEC] [--history h.json]`

Candidates: spec issues with an `assignee`, no `due_date`, and none of the
`Verify` / `Done` / `Failed` labels. Each gets `today + days`. A date that
exists is never touched, so a person's date always wins. Prints a table
(card, assignee, date, basis) on stderr; writes the YAML only when something
changed and `suggest_due` is true. History comes from GitLab
(`HISTORY_DAYS = 90` back) or from a `stats --dump` file, which is the
container path. `/board` may run it.

Ceiling, deliberate: `today + days` ignores how many cards the person
already holds. The samples are calendar time measured while people juggled
their usual load, which absorbs some of it.

## B — tight dates

`estimate.tight(history, columns, now, cfg)` is attached by the CLI's
`_summary` as `summary["flow"]["tight"]` (not inside `summarise`: `estimate`
imports `stats`, so `stats` must not import it back; renderers read it with
`.get`): open cards with an
assignee and a `due_date` of today or later (past dates are already under
*overdue*), not in Verify/Done/Failed, where the expected finish is later
than the due date. Expected finish is `max(started + days, today)` for a
card on the board and `today + days` for a Backlog card. Each row carries
`iid, title, assignee, due, expected, basis, url`. `for_person` slices it by
assignee. Shown in the team Flow section and the person's page, markdown and
HTML; `tight` (a count) joins the `stats.jsonl` row.

`_history` fetches back `max(2 * days, HISTORY_DAYS)` so a weekly digest
still has samples.

## Not in v1

A backtest of how often estimates held; weekday arithmetic (history is
calendar days, so estimates are too); a `status` column (it has no history
offline — wait for `tight` in `stats.jsonl`); a staged note per card;
`size::` buckets.

## Tests

Ladder order and the `min_samples` floor; nearest-rank values; active days
start at the first column add; config defaults and rejections; existing
dates untouched; `suggest_due: false` writes nothing; tight detection incl.
the overdue and Backlog cases; mail rows keep `bgcolor`; CLI offline via
`--history`.
