# The AI pass

```
/board group/project
/board group/project "Dev Board"
/board boards/x.yaml          # offline: the file is the board
```

The command runs `show --markdown` (or `show --from` offline), and if
`snapshots.jsonl` exists, `report` for movement, ages and the stuck list.
Any `tasks.md` in the directory is ingested first. The reply is one status
line, then only the sections that have something in them:

```
open 14 · Verify 3 (oldest 4d) · overdue 1 · staged 2 moves · 1 unposted note · 1 question
```

Questions
: Only a question that blocks a staged move; anything else is staged with
  the default and marked "reverse if wrong". Every question is also a
  `notes:` entry starting `Q:` on that issue, so the answer arrives through
  `discussion:` on the next pull rather than in a chat log.

Staged
: The `plan` table verbatim, one reason per row. A `drift` or `skipped` row
  is explained, never dropped. A rename (an entry with an `iid` whose title
  changed) says "rename". Staged notes are rows too.

Stuck
: From the report's stuck section and the `age:` suffixes: the issue, its
  column and days, what would unstick it, and the `stale` label it staged.

Hand back
: Offline only, three lines: the file to copy back,
  `gitboard sync boards/x.yaml` on the host, any `migrate-comments` lines.

No progress prose, no follow-up list, no "shall I push?": the agent stages
the moves and the plan table is the question.

## What the agent may and may not stage

- Into `Verify`, never out. `Done` and `Failed` come from a person's
  `verified:` / `failed:` comment on the card; `ingest` reads them.
- No retitle without an `iid`; with one it is a rename and is labelled so.
- `discussion:` is read-only; replies are `notes:`, posted with a
  `*staged via gitboard*` first line so the team sees which comments the
  agent staged.
- Follow-ups are the `stale` and `re-verify` labels, nothing invented.
- Nothing is deleted or closed. Supersession is a `migrate-comments` line
  for you to run.

## The write path

Exactly one: edit the board's YAML in `boards/`, run `plan`, show the table,
wait for a yes in the conversation, then `push --yes`. Never an `push`
whose `plan` you have not just seen. No MCP write tools, no direct API calls.
`push` is additive-only, which bounds the blast radius: the worst case is an
extra issue or a wrong label, never a deletion.

`migrate-comments` and `--close-source` are deliberately not in the
command's `allowed-tools`. The AI writes the line; you run it.

`status` is in the allowed tools (read-only); `sync` is not, because it
writes. Offline, none of `push`, `sync`, `pull` or `snapshot` is run.

## Revoking write access

Two things allow the write; remove either.

1. Drop `GITLAB_WRITE_TOKEN` from `.env` (and the `gitlab-write-token`
   keychain item), leaving a `read_api` token. Scope is the hard guarantee:
   `push` then fails with a one-line scope error regardless of the prompt.
2. Re-restrict `allowed-tools` in `.claude/commands/board.md` to `show`,
   `plan`, `report` and `status`.

Do both to get the old read-only guarantee back.

## The command file

```{literalinclude} ../.claude/commands/board.md
:language: markdown
```
