# remote-gitboard

Read a self-hosted GitLab issue board from the terminal, define one in YAML,
and get an AI pass over it — progress, follow-ups, open questions.

The AI pass is **read-only by design**: it can only suggest. Writes go through
`gitboard apply`, which you run.

```
test/test — Dev Board
├── Backlog (1)
│   └── #4 Rotate the PAT before it expires  unassigned
├── Doing (2)
│   ├── #3 Decide whether to add the MCP server  unassigned  Review
│   └── #1 Set up the board from YAML  @root
├── Blocked (1)
│   └── #2 Point gitboard at the work instance  unassigned  due 2026-09-01
└── Review (1)
    └── #3 Decide whether to add the MCP server  unassigned  Doing
4 issues · 3 unassigned
defined by boards/test.yaml — edit it, then `gitboard plan`
```

## Quick start

```bash
make install                       # venv + dependencies
make link                          # optional: `gitboard` in ~/.local/bin
make show PROJECT=group/project
```

`make install` is idempotent and every other target depends on it, so
`make show` on a fresh checkout does the right thing.

Without `make link`, use the `make` targets (`make show`, `make plan`,
`make apply`) or `PYTHONPATH=src .venv/bin/python -m gitboard.cli`. The rest of
this README writes `gitboard` for brevity.

## Pointing it at your instance

### 1. Mint tokens

GitLab UI → avatar → **Edit profile → Access → Personal access tokens**.

You want **two**, because the scopes differ:

| Token | Scope | Used by |
|---|---|---|
| `GITLAB_READ_TOKEN` | `read_api` | `show`, and the `/board` AI pass |
| `GITLAB_WRITE_TOKEN` | `api` | `apply` only |

`api` is the only scope that can write issues and labels — there is no
finer-grained "write issues" option, so the write token is necessarily broad
(it can delete projects). Keeping it in a separate slot is what lets the
read-only AI pass stay read-only on a machine that is also able to write. A
single `api` token in `GITLAB_READ_TOKEN` would hand write scope to
everything.

If you only have one `api` token, set `GITLAB_READ_TOKEN` and leave the write
slot unset — `apply` falls back to it. If the fallback lacks `api`, the write
is refused with a message naming the fix rather than a raw 403.

`write_repository` is **not** the one you want: it is Git-over-HTTP only and
its own description says "not using the API".

Work instances often cap token lifetime by admin policy — note the expiry.

> SSH keys cannot do this. The REST API accepts OAuth tokens, personal /
> project / group access tokens, session cookies, CI job tokens and
> impersonation tokens — not SSH keys, and explicitly not deploy tokens. The
> "mint a PAT over SSH" request is
> [issue 19672](https://gitlab.com/gitlab-org/gitlab/-/issues/19672), still
> open.

### 2. Store them

Either the macOS keychain:

```bash
security add-generic-password -a "$USER" -s gitlab-read-token  -w '<read PAT>'
security add-generic-password -a "$USER" -s gitlab-write-token -w '<api PAT>'
```

…or `.env`, which is gitignored and already exists for docker compose:

```bash
# .env
GITLAB_URL=https://gitlab.YOURCO.com
GITLAB_READ_TOKEN=glpat-…
GITLAB_WRITE_TOKEN=glpat-…
```

`.env` is the lazy option and fine for a local instance. For a work instance
the keychain is better: `.env` sits in the repo, and gitignore does not protect
against a stray `cp -r`, a backup, or a folder sync.

`gitboard config` reports which source each token actually came from.

### 3. Set defaults

```bash
cp gitboard.toml.example gitboard.toml
```

```toml
url = "https://gitlab.YOURCO.com"
project = "group/project"     # makes the argument optional
spec = "boards/team.yaml"
# board = "Dev Board"         # only if a project has several
```

Then `gitboard show` and `gitboard plan` need no arguments.

### 4. Optionally, the MCP server

```bash
curl -s -o /dev/null -w '%{http_code}\n' "$GITLAB_URL/api/v4/mcp"
```

- **Not 404** → `claude mcp add --transport http gitlab "$GITLAB_URL/api/v4/mcp"`.
  OAuth 2.0 dynamic client registration, so no second token on disk. Free
  tier, not Duo-gated. Gives Claude ~34 tools: issues, labels, comments,
  milestones, MRs, pipelines, code search.
- **404** → your instance predates it. Everything here still works; the AI
  pass just sees the board rather than the board plus discussion threads.

**No MCP server exposes board structure**, which is why this repo exists. MCP
is additive, never required.

## Commands

```bash
gitboard show group/project              # the board, as a tree
gitboard show group/project "Dev Board"  # a named board
gitboard show --all                      # do not truncate long columns
gitboard show -n 20                      # 20 issues per column
gitboard show --markdown                 # stable output, for pipes and the AI
gitboard tui group/project               # interactive: reload, snapshot, apply
gitboard plan boards/team.yaml           # what would change
gitboard apply boards/team.yaml          # write it (--yes skips the prompt)
gitboard migrate-comments 12 34          # copy #12's comments onto #34 (writes)
gitboard snapshot group/project          # append board state to snapshots.jsonl
gitboard config                          # what URL and tokens resolved
gitboard --help
```

`show` sorts overdue work to the top of each column and truncates to 5 issues
each, so a 200-issue board still fits on a screen and the truncation never
hides the part you were looking for. The footer counts issues, unassigned and
overdue, and names the YAML that defines the board.

`tui` is the loop version of `show`: the same tree, redrawn on `r`, with
`s` appending a snapshot, and — when a `boards/*.yaml` defines the board —
`p` showing spec drift and `a` applying it after a y/n on the change table.
`q` leaves.

`--url`, `--read-token` and `--write-token` override the environment for one
invocation; `-v` turns on debug logging.

**Streams:** the board goes to **stdout**, logs and progress to **stderr**, so
`gitboard show --markdown | less` stays clean.

`migrate-comments` copies discussion from a superseded issue onto its
replacement, oldest first, each prefixed `*from #12, by @alice on
2026-08-01:*` — the API cannot post as someone else, so attribution is a
header. Idempotent: already-copied comments are skipped. System notes (label
churn) are not copied.

`snapshot` appends one JSON line per open issue (timestamp, columns,
assignee, due date) to `snapshots.jsonl`. Run it on a schedule and the file
becomes a progress log you can query with jq — who moved what when — and
joined against `git log`, who ships what they pick up.

## Defining a board in YAML

`show` reads; `apply` writes. Columns and issues live in a file you edit and
re-apply:

```yaml
project: test/test
board: Dev Board
# create_project: true        # make the project if it does not exist

columns:                      # board lists, in order. Each is a label.
  - name: Doing
    color: "#428bca"
  - name: Blocked
    color: "#d9534f"

issues:
  - title: Set up the board from YAML
    labels: [Doing]
    assignee: root
    description: |
      Markdown is fine here.
  - title: Point gitboard at the work instance
    labels: [Blocked]
    due_date: 2026-09-01
  - title: Rotate the PAT      # no labels -> Backlog
```

```bash
gitboard plan boards/test.yaml     # what would change
gitboard apply boards/test.yaml    # write it
gitboard show test/test            # read it back
```

- **Idempotent** — re-running writes only the drift.
- **Additive only** — nothing is deleted or closed, so removing an issue from
  the YAML leaves it on the board.
- **Issues are matched by title**, so editing a title creates a new issue
  rather than renaming the old one.
- **Needs the `api`-scope token.** A `read_api` token reads boards perfectly
  and fails only here.
- `Backlog` is synthesised for issues carrying no column label; you never
  declare it.

Two gotchas that cost a debugging round each, both handled and both pinned by
tests: an unquoted `due_date: 2026-09-01` is a date object to YAML (not JSON
serialisable), and GitLab strips the trailing newline that a `|` block keeps —
which made every apply report a phantom description change.

## The AI pass

```
/board group/project
/board group/project "Dev Board"
```

Four sections: Progress, Needs follow-up, Questions for you, Suggested moves.
The command can also **apply** the moves, through one path only: it edits the
board's YAML, runs `plan`, shows you the pending table, and waits for a yes
in the conversation before `apply --yes`. `apply` is additive-only — nothing
is ever deleted or closed — and the YAML stays the source of truth.

## Configuration

Precedence, highest first:

```
--flag  >  environment  >  .env  >  gitboard.toml  >  built-in default
```

Both `.env` and `gitboard.toml` are found by **walking up from the current
directory**, the way git finds its root, so they work from any subdirectory.
A relative `spec` in a config file resolves against *that file's* directory,
not your cwd.

`gitboard.toml` is searched at `--config PATH` / `$GITBOARD_CONFIG`, then
`./gitboard.toml` upwards, then `~/.config/gitboard/config.toml`
(`$XDG_CONFIG_HOME` honoured). Keys: `url`, `project`, `board`, `spec`.

**There is no `token` key** in `gitboard.toml`. Tokens belong in the keychain,
the environment, or `.env`; a `token` key is ignored with a warning, because
config files get committed by accident and keychains do not. `gitboard.toml`
is gitignored regardless.

TOML via stdlib `tomllib`, so no dependency. A malformed file, or a `--config`
path that does not exist, is an error rather than a silent fallback.

## Local test instance

A disposable GitLab CE in Docker, so you can develop against a real board
without touching work. No Dockerfile — GitLab ships an official image.

```bash
open -a Docker                # daemon must be running
cp .env.example .env          # then set GITLAB_ROOT_PASSWORD
make up                       # start the container
make wait                     # block until healthy — first boot is 5-10 min
make install                  # needed before seeding
scripts/seed.py               # demo project, board, labels, issues, and a PAT
make show PROJECT=root/demo
```

| | |
|---|---|
| Web | <http://localhost:8929> — user `root`, password from `.env` |
| SSH | port `2224` (macOS Remote Login owns 22) |
| Reset | `make reset` — wipes all three volumes |

`make down` stops it and keeps the data; the container has
`restart: unless-stopped`, so it comes back after a reboot until you stop it.

**GitLab CE needs 4 GB.** Below that it hangs half-started and the logs do not
say why. Docker Desktop → Settings → Resources.

The compose file trims the instance for laptop use: 2 Puma workers, Sidekiq
concurrency 9, Prometheus/registry/KAS off. Roughly a third less RSS. Those
settings are wrong for a real deployment.

### Notes on this setup

- `external_url` and the published port must agree. Both are 8929 — change one
  and you get redirect loops.
- Volumes are named, not bind-mounted. GitLab's data dir is far too chatty for
  macOS bind mounts.
- `scripts/seed.py` sets a fixed token (`glpat-seedseed…`) so re-seeding does
  not invalidate your keychain entry. Fine for a throwaway bound to localhost,
  obviously not a pattern to carry anywhere else.
- Seeding goes through the REST API, not Rails internals — the API is stable
  across GitLab versions, `Issues::CreateService` is not. The single exception
  is minting the first token, which the API cannot bootstrap.
- **Re-running `scripts/seed.py` reverts manual board edits** back to
  `boards/demo.yaml`. That is the point of the YAML being the source of truth,
  but it will surprise you once.

## What's here

The package lives in `src/gitboard/`; `gitboard.cli:app` is the entry point.

| File | Purpose |
|---|---|
| `src/gitboard/cli.py` | The CLI. Typer + rich. |
| `src/gitboard/board.py` | Reading a board — the one gap no existing tool fills. |
| `src/gitboard/apply.py` | Making a board match YAML. The only thing that writes. |
| `src/gitboard/client.py` | The GitLab connection, and where API errors become English. |
| `src/gitboard/config.py` | Config singleton: URL, tokens, verbosity. |
| `src/gitboard/log.py` | Console + logger singletons. stdout for data, stderr for chatter. |
| `tests/` | pytest suite. No network — the API surface is faked. |
| `boards/*.yaml` | Board definitions — columns and issues, editable. |
| `scripts/seed.py` | Mints a PAT, then applies `boards/demo.yaml`. |
| `.claude/commands/board.md` | The `/board` prompt. Read-only instructions. |
| `docker-compose.yml` | Disposable local GitLab CE for development. |

`make` on its own lists every target.

### Why these packages

- **[typer](https://typer.tiangolo.com/)** — the CLI. Commands are plain
  functions with type hints; `--help`, parsing and env-var binding come free.
  Click underneath, with rich formatting on top.
- **[rich](https://rich.readthedocs.io/)** — the tree, tables, spinners and log
  handler. It detects a pipe and drops colour automatically, so there is no
  `--no-color` flag to maintain.
- **[python-gitlab](https://python-gitlab.readthedocs.io/)** — the API wrapper
  `board.py` and `apply.py` are thin shells over.
- **[pyyaml](https://pyyaml.org/)** — board definitions.
- **[python-dotenv](https://github.com/theskumar/python-dotenv)** — reads
  `.env`. Hand-rolling it is ten lines that quietly mishandle quotes and
  `export` prefixes, and `.env` is shared with docker compose, so matching
  compose's interpretation matters more than saving a dependency.

Nothing else. `pydantic` for six config fields, `structlog` on top of a logger
with one handler, or `click` alongside typer would all be weight without a job.

## Development

Plain virtualenv and pip — no uv, no Poetry, no lockfile.

```bash
make install      # python3 -m venv .venv && pip install ".[dev]"
make activate     # subshell with .venv active (exit to leave)
make test         # pytest
make lint         # ruff
make fmt          # ruff format (black-equivalent)
```

- **Editing code needs no reinstall.** Every target runs
  `PYTHONPATH=src .venv/bin/python -m gitboard.cli`, so `src/` is always what
  executes.
- **Point your editor at `.venv/bin/python`** for autocomplete. You never need
  to activate it manually; `make activate` opens a subshell if you want one.
- **Versions are not pinned.** Five well-behaved dependencies. If you ever need
  reproducibility, `.venv/bin/pip freeze > requirements.txt` is the whole
  story.
- **`make link`** writes a `~/.local/bin/gitboard` wrapper that execs this
  repo, so the global command runs current source with nothing to keep in sync.
  `make unlink` removes it. (`pipx install .` works too, if you would rather
  have an independent copy — but it goes stale when you edit the source.)

### Do not use an editable install here

```
ModuleNotFoundError: No module named 'gitboard'
```

`pip install -e .` wires the package up through a `.pth` file in `.venv` that
adds `src/` to the import path. On this machine that stops working within
about eight seconds of the install: the file is present and readable, its
target exists, `site` lists it in the directory, and the path still is not
added. Reproduced identically with pip and with uv, so it is not a packaging
tool problem. **Root cause unknown.**

Everything here avoids that mechanism rather than fighting it — hence
`PYTHONPATH=src` throughout, which also happens to make edits live. Do not
"simplify" it away.

## Why this is so small

Almost all of it already existed and is not worth rewriting:

- **[Official GitLab MCP server](https://docs.gitlab.com/user/model_context_protocol/mcp_server/)**
  — issues, labels, comments, milestones, search. Free tier, OAuth.
- **[`glab`](https://docs.gitlab.com/editor_extensions/gitlab_cli/)** — official
  CLI, has `glab issue board view` if you want a non-AI look.
- **[rcieri/glab-tui](https://github.com/rcieri/glab-tui)** — full TUI over
  `glab`/`gh`, with bulk label/assignee editing. Worth a look if you would
  rather drive a UI than a prompt.

The genuine gap: **no MCP server exposes board structure.** Boards are lists
bound to labels, and neither the official server nor the community ones read
that mapping. Hence `board_columns()`, and nothing more.

## Taking write access away again

The AI pass writes because two things allow it — revoke either:

1. Re-restrict `allowed-tools` in `.claude/commands/board.md` to `show` and
   `plan`, and put its read-only paragraph back.
2. Remove `GITLAB_WRITE_TOKEN` from `.env`, leaving a `read_api`-scope token.
   The scope is the hard guarantee: without an `api` token, `apply` fails
   with a one-line scope error no matter what the prompt says.

For incremental label moves on issues you refuse to put in YAML,
[k1sina/gitlab-mcp-server](https://github.com/k1sina/gitlab-mcp-server) with
`GITLAB_ENABLE_WRITES=true` has `update_issue`; not wired up here because the
YAML flow covers board management without a second write path.

## Notes

- **GitLab answers 404 for private projects you cannot see**, identical to a
  project that does not exist. "No project X, or the token cannot see it" is
  genuinely ambiguous, and the ambiguity is GitLab's. If the path is right, it
  is almost always token scope.
- An issue labelled for two columns appears in both. The web UI behaves the
  same way, so gitboard does not invent a tiebreak. It is counted once in the
  totals.
- Board *deletion* is unsupported on GitLab CE. Creation and list management
  are fine.
- Unattempted: webhook-driven notification. GitLab fires issue events on label
  change, so if you later want push instead of on-demand, that is native — a
  receiver, not a poller.
