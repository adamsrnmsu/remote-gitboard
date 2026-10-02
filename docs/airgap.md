# Working where GitLab is unreachable

The setup this page assumes: a Linux container with Claude Code, git, python,
and this repo checked out, but **no route to GitLab**. Files move in and out
by hand (a bind mount or `docker cp`). The host can reach GitLab and does
every read and write; the container thinks.

The YAML is the staged change. That is the whole trick: everything the
container produces is a file diff the host can `plan` against the live board
before writing.

## What "unreachable" costs

| Needs GitLab | Works from files only |
|---|---|
| `show`, `plan`, `push`, `sync`, `pull`, `snapshot`, `status`, `tui`, `migrate-comments`, `stats`, `digest` | `show --from`, `plan --against`, `tui --from`, `report`, `ingest`, `stats --from`, `digest --from`, `bd` |

Nothing in the right-hand column opens a connection or looks for a token.

## Host, before

```bash
gitboard pull group/project --base --notes    # boards/x.yaml + boards/x.yaml.base + a snapshot
```

`--base` keeps an untouched copy for `plan --against`. `--notes` includes each
issue's discussion as a read-only `discussion:` list, so the agent sees the
team's comments, not just the labels. `pull` also appends to
`snapshots.jsonl`, so `report` (and the `age:` suffixes in `show`) work
inside without a separate `snapshot`. `pull --all` does every board in
`boards/`.

If `boards/x.yaml` already has edits that were never pushed, `pull --force`
refuses rather than lose them; `--discard-edits` is the explicit override.

**Beads.** `bd` needs git only for `bd init`, so initialise on the host and
copy `.beads/` in. Inside, `bd metrics off`, set `BD_NON_INTERACTIVE=1`, and
do not run `bd doctor` (it wants the network).

**Python dependencies.** If the container has a pip index, `make install`
works as usual. If not, vendor wheels on the host:

```bash
.venv/bin/python -m pip download --dest wheels --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.11 --implementation cp python-gitlab pyyaml typer rich python-dotenv
```

arm64: `manylinux2014_aarch64`. `--python-version` must match the
container's interpreter. glibc containers only (no Alpine/musl). Then inside:

```bash
python3 -m venv .venv && .venv/bin/pip install --no-index --find-links wheels/ python-gitlab pyyaml typer rich python-dotenv
```

**bd binary.** The Linux tarball, e.g. `beads_1.3.0_linux_amd64.tar.gz` from
<https://github.com/gastownhall/beads/releases>. glibc-linked, single
binary, no other files needed.

**Claude Code on a locked-down box.** Per
<https://code.claude.com/docs/en/network-config> and
<https://code.claude.com/docs/en/env-vars>:

```bash
export HTTPS_PROXY=http://proxy:3128            # if API egress goes through one
export ANTHROPIC_BASE_URL=https://...           # if a gateway fronts the API
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
export DISABLE_AUTOUPDATER=1
```

**Never copy `.env` in.** The container has nothing to do with a token, and
the read-only guarantee inside is that there is no token to find.

## Manifest

| In | Out |
|---|---|
| repo checkout (no `.env`) | `boards/x.yaml` (edited) |
| `boards/x.yaml`, `boards/x.yaml.base` | `issues.jsonl` (`bd export`) |
| `snapshots.jsonl` | any `tasks.md` the agent rewrote |
| `.beads/` (host-initialised) | the hand-back list of host commands |
| `wheels/` (if no pip index) | |
| `bd` binary | |
| `tasks.md` files to ingest | |
| a project `CLAUDE.md` for the container, see {doc}`agent-briefing` | |

## Inside

```bash
/board boards/x.yaml                                   # the AI pass, offline
gitboard show --from boards/x.yaml                     # the board, from the file
gitboard plan boards/x.yaml --against boards/x.yaml.base   # what is staged
gitboard tui --from boards/x.yaml                      # r reload, e edit, p diff, q
gitboard ingest tasks.md --into boards/x.yaml          # tasks -> issues
bd ready                                               # the agent's own queue
```

The agent edits `boards/x.yaml` only: labels, assignees, due dates, new
issues without `iid`, and `notes:` as replies to the team. It never runs
`push`.

## Host, after

Copy `boards/x.yaml` (and `issues.jsonl`) back. Then:

```bash
gitboard sync boards/x.yaml     # plan, y/n, push, snapshot, rotate the base
bd import issues.jsonl          # additive
```

`sync` is the four host steps in one: `plan` against the live board (a
three-way merge with `x.yaml.base`), a y/n on the table, `push`, a snapshot,
and `x.yaml.base` replaced by the post-push state (the old one kept as
`x.yaml.base.old`). `--yes` skips the prompt. The three-way plan is the
safety net: an issue the team moved while the container was thinking shows
as a `drift` row, kept as the team left it, and `sync` stops on drift unless
you pass `--ignore-drift`. `plan boards/x.yaml` and `push boards/x.yaml`
remain available as the separate steps.

Inside, export with `bd export --all -o issues.jsonl`. Import is additive
and deletions do not propagate, so close beads instead of deleting them.

Run any `migrate-comments` lines from the Hand back section yourself.

## Next round: what the team did

```bash
gitboard status                                                # every board: pulled ago, staged, notes, oldest in Verify, overdue
gitboard pull group/project --force --base --notes             # rotates the old base to x.yaml.base.old
gitboard plan boards/x.yaml --against boards/x.yaml.base.old   # what moved on GitLab
gitboard report group/project --since boards/x.yaml            # from the snapshot log, since that pull; plus the stuck section
```

`pull --base` rotates: the previous `x.yaml.base` becomes `x.yaml.base.old`
before the new one is written, so the `--against` diff of the new pull
versus the old base is the team's changes, label by label. `status` is the
glance before deciding whether a round is worth starting. Then the round
starts again from [Host, before](#host-before).
