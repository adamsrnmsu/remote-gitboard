# gitboard replay: a timelapse of the board

## Goal

`gitboard replay [PROJECT] [--days N | --since SPEC] [--out replay.html]`
turns `snapshots.jsonl` into one self-contained HTML page that plays the board
back: columns as lanes, cards as boxes that slide between lanes as the
snapshots say they moved, fade in when new and fade out when they leave the
open set (closed).

## Non-goals

- No network, no token, no GitLab call. The log is the only input.
- No per-person tallies, rates or ranking. Assignee is a card attribute only.
- No interpolation between snapshots: one frame per distinct board state.
- No history before the first snapshot, no label/milestone/estimate data
  (snapshots do not carry them).
- No server, no CDN, no vendored library.

## Data

`report.load(db, project, days)` gives batches (`{iid: record}`, oldest first);
`--since SPEC` uses `report.since` with SPEC.base's mtime, like `report`.
Without PROJECT the log must hold exactly one project, else the command lists
them and exits 1 (iids collide across projects).

`replay.frames(batches)` returns `(frames, cards)`:

- `frames`: `[{ts, columns: {name: [iid, ...]}, moved, new, closed}]`.
  `columns` iids are sorted; a two-column card is in both lists. `moved/new/
  closed` are counts against the previous kept frame (0/0/0 for the first;
  `new` and `closed` follow `report.diff`'s meaning: appeared / gone from the
  open set).
- A frame whose `columns` equal the previous kept frame's is dropped.
- `cards`: `{iid: {title, assignee, due_date}}`, latest values seen.
- Column order: first seen across batches, Backlog first when present.

`replay.render_html(frames, cards, title, base_url=None, project=None)`. A
card links to `base_url/project/-/issues/iid` only when `base_url` is http(s);
otherwise no link (snapshots carry no `web_url`).

## Page behaviour

- One lane per column, coloured from `mail.COLUMN_COLORS` (`mail._colour`'s
  extra-column fallback). Cards are absolutely positioned boxes, one per
  (card, lane); moves animate with CSS transitions on `transform`/`opacity`.
- Controls: play/pause, a `<input type=range>` scrubber over frames, speed
  select (0.5x/1x/2x/4x), the frame timestamp, and "moved N, new N, closed N".
  Space toggles play; the scrubber works while paused. Stops on the last
  frame.
- Light/dark via `prefers-color-scheme`; inline CSS and JS only.
- Respects reduced motion: transitions off under `prefers-reduced-motion`.

## Safety

- The embedded data goes through graph_html's `_json` (`<`, `>`, `&` escaped),
  so a title of `</script>` cannot end the script.
- JS writes titles with `textContent` and links via `setAttribute` only for
  URLs Python already checked as http(s). The page `<title>` goes through
  `html.escape`.
- No per-person figures anywhere; the assignee shows on the card and in its
  tooltip only.

## Errors

Fewer than 2 batches (after the window and dedupe of nothing: raw batches)
prints `need at least 2 snapshots in the window; have N` and exits 1. A log
whose frames all collapse to one still renders (the board did not change).

## Tests

- `frames`: move, new, close, two-column card, identical frames collapsed,
  column order (Backlog first, then first-seen), latest card values.
- `render_html`: title escaped, `</script>` title cannot break out (exactly
  one closing `</script>` inside body), non-http base gives no link, no CDN.
- CLI: writes the file with synthetic snapshots; exits 1 below 2 batches and
  on a missing log; multiple projects without PROJECT errors.
