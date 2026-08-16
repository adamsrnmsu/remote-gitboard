# remote-gitboard

Read a self-hosted GitLab issue board from the terminal and get an AI pass over
it — progress, follow-ups, open questions. **Read-only by design.**

## Resume here

Everything that could be done without your GitLab instance is done. Three steps
left, all of them need you.

### 1. Create a personal access token

GitLab UI → avatar → **Edit profile → Access → Personal access tokens**.

- Scope: **`read_api`** only. Not `api`. The read-only guarantee is enforced by
  the token, not by the prompt.
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
./board.py group/project        # should print your board as markdown
```

Then check whether your instance exposes the official MCP server:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "$GITLAB_URL/api/v4/mcp"
```

- **Not 404** → `claude mcp add --transport http gitlab "$GITLAB_URL/api/v4/mcp"`.
  OAuth 2.0 dynamic client registration, so no second token on disk. Free tier,
  not Duo-gated. Gives Claude ~34 tools: issues, labels, comments/notes,
  milestones, MRs, pipelines, code search.
- **404** → instance predates it. `board.py` alone still works; the AI pass just
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
./seed.py                          # demo project, board, labels, issues, PAT
```

`seed.py` prints the two commands that point `board.py` at it. Then:

```bash
./board.py root/demo
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

```
/board group/project
/board group/project "Dev Board"
```

Prints four sections: Progress, Needs follow-up, Questions for you, Suggested
moves. Suggestions are copyable, never applied.

Or use the script bare, for piping or a quick look:

```bash
./board.py group/project | less
```

## What's here

| File | Purpose |
|---|---|
| `board.py` | Dumps a board as markdown. The one gap no existing tool fills. |
| `.claude/commands/board.md` | The `/board` prompt. Read-only instructions. |
| `docker-compose.yml` | Disposable local GitLab CE for development. |
| `seed.py` | Mints a PAT and seeds a demo board on that instance. |

`./board.py --selftest` runs the column-bucketing check with no deps installed.

## Why this is so small

Almost all of it already existed and is not worth rewriting:

- **[Official GitLab MCP server](https://docs.gitlab.com/user/model_context_protocol/mcp_server/)**
  — issues, labels, comments, milestones, search. Free tier, OAuth.
- **[`python-gitlab`](https://python-gitlab.readthedocs.io/en/stable/gl_objects/boards.html)**
  — the API wrapper `board.py` is a thin shell over.
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
   keep refusing to write even with the tools present.

## Notes

- An issue labeled for two columns appears in both. The GitLab web UI behaves
  the same way, so `board.py` doesn't invent a tiebreak.
- Board *deletion* is unsupported on GitLab CE. Creation and list management
  are fine.
- Unattempted: webhook-driven notification. GitLab fires issue events on label
  change, so if you later want push instead of on-demand, that's native — a
  receiver, not a poller.
