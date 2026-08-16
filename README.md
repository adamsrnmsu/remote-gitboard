# remote-gitboard

Read a self-hosted GitLab issue board from the terminal and get an AI pass over
it — progress, follow-ups, open questions. **Read-only by design.**

## Resume here

Everything that could be done without your GitLab instance is done. Three steps
left, all of them need you.

### 1. Create a personal access token

GitLab UI → avatar → **Edit profile → Access → Personal access tokens**.

- Scope: **`read_api`** only. Not `api`. The read-only guarantee is enforced by
  the token, not by the prompt. This is the one that goes in `GITLAB_READ_TOKEN`.
- If you also want `apply` to write, mint a **second** token with `api` scope
  and put that one in `GITLAB_WRITE_TOKEN` (or a `gitlab-write-token` keychain
  item). Two tokens, so reading can never write.
- Work instances often cap token lifetime by admin policy — note the expiry.

SSH keys cannot do this. The GitLab REST API accepts OAuth tokens, personal /
project / group access tokens, session cookies, CI job tokens and impersonation
tokens — not SSH keys, and explicitly not deploy tokens. The "mint a PAT over
SSH" request is [issue 19672](https://gitlab.com/gitlab-org/gitlab/-/issues/19672),
still open and unimplemented.

### 2. Store it and point at your instance

```bash
security add-generic-password -a "$USER" -s gitlab-token -w '<PAT>'
echo 'export GITLAB_URL=https://gitlab.YOURCO.com' >> ~/.zshrc && source ~/.zshrc
```

The keychain is the default lookup, so nothing lands in a dotfile. `GITLAB_TOKEN`
in the environment overrides it if you'd rather pass one per-shell.

### 3. Verify, then decide on MCP

```bash
gitboard show group/project   # should print your board
```

Then check whether your instance exposes the official MCP server:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "$GITLAB_URL/api/v4/mcp"
```

- **Not 404** → `claude mcp add --transport http gitlab "$GITLAB_URL/api/v4/mcp"`.
  OAuth 2.0 dynamic client registration, so no second token on disk. Free tier,
  not Duo-gated. Gives Claude ~34 tools: issues, labels, comments/notes,
  milestones, MRs, pipelines, code search.
- **404** → instance predates it. `gitboard` alone still works; the AI pass just
  sees the board rather than the board plus issue discussion threads.

## Local test instance

A disposable GitLab CE in Docker, so you can develop against a real board
without touching work. No Dockerfile — GitLab ships an official image, there is
nothing to build.

```bash
open -a Docker                     # daemon must be running
cp .env.example .env               # then edit the password
docker compose up -d
docker compose logs -f gitlab      # wait for healthy — first boot is 5-10 min
scripts/seed.py                          # demo project, board, labels, issues, PAT
```

`scripts/seed.py` prints the two commands that point gitboard at it. Then:

```bash
gitboard show root/demo
```

| | |
|---|---|
| Web | <http://localhost:8929> — user `root`, password from `.env` |
| SSH | port `2224` (macOS Remote Login owns 22) |
| Reset | `docker compose down -v` — wipes all three volumes |

**Memory is already fine here** — the Docker VM has 8.3 GB and 12 CPUs, well
over GitLab's 4 GB floor. Worth knowing anyway: below 4 GB, GitLab CE hangs
half-started and the logs don't say why. Docker Desktop → Settings → Resources.

The compose file trims the instance for laptop use: 2 Puma workers, Sidekiq
concurrency 9, Prometheus/registry/KAS off. Roughly a third less RSS. Those
settings are wrong for a real deployment.

### Notes on this setup

- `external_url` and the published port must agree. Both are 8929 — change one
  and you get redirect loops.
- Volumes are named, not bind-mounted. GitLab's data dir is far too chatty for
  macOS bind mounts.
- `seed.py` sets a fixed token (`glpat-seedseed…`) so re-seeding doesn't
  invalidate your keychain entry. Fine for a throwaway on localhost, obviously
  not a pattern to carry anywhere else.
- Seeding goes through the REST API via `python-gitlab`, not Rails internals —
  the API is stable across GitLab versions, `Issues::CreateService` is not. The
  single exception is minting the first token, which the API can't bootstrap.

### Docker version

Docker Desktop 4.86.0 — engine `29.7.2`, Compose `v5.3.1`. Upgraded from a 2021
install (Desktop 3.5.2 / engine 20.10.7 / compose v2.0.0-beta.6) that had
stopped working on macOS 26: the daemon refused socket connections and the CLI
crashed outright.

The old install was removed cleanly, so there are no images cached locally —
first `docker compose up` pulls the ~3 GB GitLab image.

`/usr/local/bin/docker-compose` is left over from the old install and dangles;
Compose v2+ is a subcommand (`docker compose`) and no longer ships a standalone
binary. Nothing here calls it. To clear it:

```bash
sudo rm /usr/local/bin/docker-compose /usr/local/bin/docker-compose-v1
```

## Use

```bash
make install             # one-time setup: venv + dependencies

gitboard show group/project              # the board, as a tree
gitboard show group/project "Dev Board"  # a named board
gitboard show group/project --markdown   # stable output, for pipes
gitboard plan boards/test.yaml           # what would change
gitboard apply boards/test.yaml          # write it
gitboard config                          # what URL and token it resolved
gitboard --help
```

`--url`, `--read-token`, and `--write-token` override `GITLAB_URL`,
`GITLAB_READ_TOKEN`, and `GITLAB_WRITE_TOKEN` per invocation;
`-v` turns on debug logging. Logs and progress go to **stderr**, the board
goes to **stdout**, so `--markdown | less` stays clean.

### Credentials in .env

The lazy option, and fine for a local instance. `.env` is already gitignored
and already exists for docker compose:

```bash
# .env
GITLAB_URL=http://localhost:8929
GITLAB_READ_TOKEN=glpat-read…     # read_api — show, and the /board pass
GITLAB_WRITE_TOKEN=glpat-write…   # api      — apply only
```

**Two tokens, because the scopes differ.** Reading a board needs `read_api`;
`apply` needs `api`. Keeping them in separate slots is what lets the read-only
AI pass stay read-only on a machine that is also able to write — a single
`api` token in `GITLAB_READ_TOKEN` would hand write scope to everything.

If you only have one `api`-scope token, set `GITLAB_READ_TOKEN` and leave the
write slot unset: `apply` falls back to it. If the fallback token lacks `api`, the
write is refused with a message naming the fix rather than a raw 403.

Then everything works with no exports and no keychain:

```bash
gitboard show test/test
```

Found by walking up from the current directory, the way git finds its root —
so it works from any subdirectory of a project. A real exported variable
still wins over it, and `gitboard config` reports which of the four
sources the token actually came from.

`.env` is the one file allowed to hold a token, because it is gitignored and
never meant to be shared. `gitboard.toml` still refuses one. For a work
instance the keychain is better — `.env` sits in the repo, and a stray
`cp -r` or a backup takes the token with it.

### Config file

So you stop exporting `GITLAB_URL` in every shell:

```bash
cp gitboard.toml.example gitboard.toml   # then edit
```

```toml
url = "https://gitlab.YOURCO.com"
project = "group/project"     # makes the argument optional
spec = "boards/team.yaml"
# board = "Dev Board"         # only if a project has several
```

```bash
gitboard show      # project comes from the file
gitboard plan      # spec comes from the file
gitboard config    # shows what resolved, and from where
```

Searched in order, first hit wins:

1. `--config PATH`, or `$GITBOARD_CONFIG`
2. `./gitboard.toml` — per project
3. `~/.config/gitboard/config.toml` — per user (`$XDG_CONFIG_HOME` honoured)

Precedence is **`--flag` > environment > `.env` > `gitboard.toml` > default**,
so the file sets your normal instance and a flag still overrides it for one
command.

**There is no `token` key.** A PAT belongs in the keychain, in `GITLAB_READ_TOKEN`,
or behind `--read-token`; a `token` key in the file is ignored with a warning,
because config files get committed by accident and keychains don't.
`gitboard.toml` is gitignored regardless.

TOML via stdlib `tomllib` — no dependency added. A malformed file, or a
`--config` path that doesn't exist, is an error rather than a silent fallback.

Or the AI pass:

```
/board group/project
/board group/project "Dev Board"
```

Prints four sections: Progress, Needs follow-up, Questions for you, Suggested
moves. Suggestions are copyable, never applied.

## What's here

The package lives in `src/gitboard/`; `gitboard.cli:app` is the entry point.

| File | Purpose |
|---|---|
| `src/gitboard/cli.py` | The CLI. Typer + rich. The entry point. |
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

### Why these packages

- **[typer](https://typer.tiangolo.com/)** — the CLI. Commands are plain
  functions with type hints; `--help`, parsing, and env-var binding come free.
  It is Click underneath, with rich formatting on top.
- **[rich](https://rich.readthedocs.io/)** — the tree, tables, spinners, and
  log handler. It detects a pipe and drops colour automatically, so there is
  no `--no-color` flag to maintain.
- **[python-dotenv](https://github.com/theskumar/python-dotenv)** — reads
  `.env`. Hand-rolling this is ten lines that quietly mishandle quotes and
  `export` prefixes, and `.env` is shared with docker compose, so matching
  compose's interpretation matters more than saving a dependency.

Nothing else was added. `pydantic` for four config fields, `structlog` on top
of a logger with one handler, or `click` alongside typer would all be weight
without a job here.

## How the environment works

Plain Python tooling — a virtualenv and pip, nothing exotic.

```bash
make install      # python3 -m venv .venv && pip install ".[dev]"
```

`pyproject.toml` declares the dependencies, the dev extras (pytest, ruff), and
the `gitboard` command:

```toml
[project.scripts]
gitboard = "gitboard.cli:app"
```

- **`make install`** is idempotent and every other target depends on it, so
  `make test` or `make show` sets the environment up on a clean checkout.
- **Editing code needs no reinstall.** The targets run
  `PYTHONPATH=src .venv/bin/python -m gitboard.cli`, so `src/` is always what
  runs. See "Do not use an editable install here" above for why it is written
  that way rather than with `pip install -e .`.
- **Point your editor at `.venv/bin/python`** for autocomplete. You never need
  to activate it.
- **`make link`** if you want a `gitboard` command outside this repo — a
  wrapper in `~/.local/bin` pointing back here. No second install to keep in
  sync, and nothing beyond pip is needed. (`pipx install .` also works, if you
  would rather have a fully independent copy.)
- **Versions are not pinned.** There is no lockfile; the dependency set is five
  well-behaved packages. If you ever need reproducibility,
  `.venv/bin/pip freeze > requirements.txt` is the whole story.

## Defining a board in YAML

`show` reads; `apply` writes. Columns and issues live in a YAML file you edit
and re-apply:

```yaml
project: test/test
board: Dev Board
columns:
  - name: Doing
    color: "#428bca"
  - name: Blocked
    color: "#d9534f"
issues:
  - title: Set up the board from YAML
    labels: [Doing]
    assignee: root
  - title: Point gitboard at the work instance
    labels: [Blocked]
    due_date: 2026-09-01
  - title: Rotate the PAT      # no labels -> Backlog
```

```bash
gitboard plan boards/test.yaml     # what would change
gitboard apply boards/test.yaml    # write it
gitboard show test/test           # read it back
```

Idempotent — re-running writes only the drift. **Additive only:** nothing is
deleted or closed, so removing an issue from the YAML leaves it on the board.
Issues are matched by **title**, so editing a title creates a new issue rather
than renaming the old one.

This needs an `api`-scope token, not the `read_api` one reading uses. Two
gotchas worth knowing, both now handled: quote a `due_date` or don't, either
works, and a `|` block description won't report a phantom change on every run.

`make test` runs the suite.

## Why this is so small

Almost all of it already existed and is not worth rewriting:

- **[Official GitLab MCP server](https://docs.gitlab.com/user/model_context_protocol/mcp_server/)**
  — issues, labels, comments, milestones, search. Free tier, OAuth.
- **[`python-gitlab`](https://python-gitlab.readthedocs.io/en/stable/gl_objects/boards.html)**
  — the API wrapper `board.py` and `apply.py` are thin shells over.
- **[`glab`](https://docs.gitlab.com/editor_extensions/gitlab_cli/)** — official
  CLI, has `glab issue board view` if you want a non-AI look.
- **[rcieri/glab-tui](https://github.com/rcieri/glab-tui)** — full TUI over
  `glab`/`gh`, with bulk label/assignee editing. Worth a look if you'd rather
  drive a UI than a prompt.

The genuine gap: **no MCP server exposes board structure.** Boards are lists
bound to labels, and neither the official server nor the community ones read
that mapping. Hence `board.py`, and nothing more.

## Adding writes later

Currently the AI can only suggest. To let it actually move cards:

1. Reissue the PAT with `api` scope instead of `read_api`.
2. Add [k1sina/gitlab-mcp-server](https://github.com/k1sina/gitlab-mcp-server)
   with `GITLAB_ENABLE_WRITES=true`. It has `update_issue` with incremental
   label add/remove — the move-card primitive. The official server lacks it;
   its write set is create-only (`create_issue`, `create_workitem_note`, …).
3. Drop the read-only paragraph from `.claude/commands/board.md`, or it will
   keep refusing to write even with the tools present. (`gitboard apply`
   already writes — this is only about letting the AI do it unprompted.)

## Notes

- An issue labeled for two columns appears in both. The GitLab web UI behaves
  the same way, so gitboard doesn't invent a tiebreak.
- Board *deletion* is unsupported on GitLab CE. Creation and list management
  are fine.
- Unattempted: webhook-driven notification. GitLab fires issue events on label
  change, so if you later want push instead of on-demand, that's native — a
  receiver, not a poller.
