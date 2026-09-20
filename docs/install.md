# Install

```bash
make install     # python3 -m venv .venv && pip install ".[dev,docs]"
make link        # optional: ~/.local/bin/gitboard, execs this checkout
```

Every `make` target depends on `install`, so `make show PROJECT=g/p` on a
fresh clone works.

Without `make link`, the CLI is:

```bash
PYTHONPATH=src .venv/bin/python -m gitboard.cli show group/project
```

That is not a shortcut to tidy up. `pip install -e .` stops resolving on this
machine seconds after install (see {doc}`development`), so every target names
`src/` directly. It also means edits are live with no reinstall.

## Tokens

GitLab: avatar, **Edit profile**, **Access**, **Personal access tokens**. Mint two:

| Slot | Scope | Used by |
|---|---|---|
| `GITLAB_READ_TOKEN` | `read_api` | `show`, `pull`, `snapshot`, `tui`, the `/board` AI pass |
| `GITLAB_WRITE_TOKEN` | `api` | `apply`, `migrate-comments` |

`api` is the only scope that writes issues and labels; it is broad, which is
why it lives in its own slot. One `api` token in `GITLAB_READ_TOKEN` and the
write slot unset is also valid: `apply` falls back to the read token, and a
`read_api` token failing there gives a one-line scope error, not a 403 trace.

`write_repository` is Git-over-HTTP only. SSH keys and deploy tokens cannot
call the API at all.

### Make the agent's writes visible: a project access token

A personal `api` token makes every `apply` look like you. Use a **project
access token** for `GITLAB_WRITE_TOKEN` instead: GitLab creates a bot user
for it (`project_123_bot_...`), so every label move and every
`*staged via gitboard*` note the AI pass posts is attributed to the bot, and
the team can tell at a glance what a person did and what the agent staged.

Mint it: project, **Settings**, **Access tokens**, **Add new token**. Name it
`gitboard`, scope `api` (the only scope that writes issues and labels), role
**Reporter** (enough to edit issues, labels and comments; `Developer` if the
board YAML also creates labels or the project). Set the expiry your instance
allows and note it. Then:

```bash
GITLAB_READ_TOKEN=glpat-...    # your read_api PAT: the board is read as you
GITLAB_WRITE_TOKEN=glpat-...   # the project token: writes are the bot
```

Project tokens need Premium on gitlab.com; on a self-hosted CE instance they
are free. The bot user counts as a member with the role you chose, so it is
listed under **Members** and can be removed there, which is also the
fastest way to revoke the agent's write access.

### Keychain (macOS)

```bash
security add-generic-password -a "$USER" -s gitlab-read-token  -w '<read_api PAT>'
security add-generic-password -a "$USER" -s gitlab-write-token -w '<api PAT>'
```

### `.env`

```bash
GITLAB_URL=https://gitlab.example.com
GITLAB_READ_TOKEN=glpat-...
GITLAB_WRITE_TOKEN=glpat-...
```

Gitignored, and shared with docker compose. Fine for a local instance; for a
work instance prefer the keychain, since gitignore does not survive `cp -r`
or a folder sync.

Resolution order: `--read-token` / `--write-token`, then the env var, then
`.env`, then the keychain item. `GITLAB_TOKEN` and the `gitlab-token` keychain
item still work as pre-rename fallbacks (the env var warns). The write slot
falls back to the read token when empty.

## `gitboard.toml`

```bash
cp gitboard.toml.example gitboard.toml
```

```toml
url = "https://gitlab.example.com"
project = "group/project"    # makes the argument optional
spec = "boards/team.yaml"    # relative to this file, not your cwd
# board = "Dev Board"        # only if the project has several
# guide = false              # hide the TUI's per-mode guide panels
```

Found by walking up from the cwd (also `--config PATH`, `$GITBOARD_CONFIG`,
then `~/.config/gitboard/config.toml`). Precedence:

```
--flag  >  environment  >  .env  >  gitboard.toml  >  default
```

A `token` key in the TOML is ignored with a warning: that file is meant to be
shared. Gitignored anyway.

## Check what resolved

```bash
gitboard config
```

Prints the URL, the project/spec defaults, and which source each token came
from, without printing the tokens.
