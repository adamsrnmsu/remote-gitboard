# The AI pass

```
/board group/project
/board group/project "Dev Board"
/board boards/x.yaml          # offline: the file is the board
```

The command runs `show --markdown` (or `show --from` offline), and if
`snapshots.jsonl` exists, `report`. It answers in four sections:

Progress
: What moved and what sat still, grounded in `report` when there are two
  snapshots; otherwise the shape of the board.

Needs follow-up
: Quiet, misfiled, unassigned-in-Doing, overdue, or blocked with no
  unblocker. `#iid`, one line, the label it would add. It does not add it.

Questions for you
: Only what changes the recommendation. None is a valid answer.

Suggested moves
: `#iid: Doing -> Blocked (reason)`. Supersession is offered as a
  `gitboard migrate-comments OLD NEW --close-source` line for you to run.

Hand back
: Offline only. The `plan` and `apply` lines to run on the host, plus any
  `migrate-comments` lines.

## The write path

Exactly one: edit the board's YAML in `boards/`, run `plan`, show the table,
wait for a yes in the conversation, then `apply --yes`. Never an `apply`
whose `plan` you have not just seen. No MCP write tools, no direct API calls.
`apply` is additive-only, which bounds the blast radius: the worst case is an
extra issue or a wrong label, never a deletion.

`migrate-comments` and `--close-source` are deliberately not in the
command's `allowed-tools`. The AI writes the line; you run it.

Offline, `apply` is never run. Identity is the title, so the AI never
retitles, never invents or edits an `iid`, and leaves `iid` off issues it
adds.

## Revoking write access

Two things allow the write; remove either.

1. Drop `GITLAB_WRITE_TOKEN` from `.env` (and the `gitlab-write-token`
   keychain item), leaving a `read_api` token. Scope is the hard guarantee:
   `apply` then fails with a one-line scope error regardless of the prompt.
2. Re-restrict `allowed-tools` in `.claude/commands/board.md` to `show`,
   `plan` and `report`.

Do both to get the old read-only guarantee back.

## The command file

```{literalinclude} ../.claude/commands/board.md
:language: markdown
```
