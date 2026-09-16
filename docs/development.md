# Development

```bash
make test        # PYTHONPATH=src .venv/bin/pytest -q
make lint        # ruff check
make fmt         # ruff format
make docs        # sphinx-build -W into docs/_build/html
make activate    # subshell with .venv on PATH (exit to leave)
```

Single test: `PYTHONPATH=src .venv/bin/pytest tests/test_board.py -k backlog`.

Plain venv and pip. No uv (removed after `uv run` proved unreliable here),
no lockfile. Five dependencies; `pip freeze` is the whole story if you ever
need pins.

## No editable install

`pip install -e .` wires the package through a `.pth` file in `.venv`. On
this machine it stops being honoured within seconds of install: the file is
present, its target exists, `site` lists it, and `gitboard` still does not
import. Same with pip and uv, so not a packaging-tool bug. Root cause unknown.

Every target therefore runs `PYTHONPATH=src .venv/bin/python -m gitboard.cli`,
and `make link` writes a wrapper that does the same. Edits are live. Do not
"simplify" the PYTHONPATH away.

## Tests never hit the network

The API surface is faked. An autouse fixture in `tests/test_config.py`
isolates `XDG_CONFIG_HOME`, chdirs somewhere empty, and resets the config
singleton on both sides. Without it the repo's own `.env` and the developer's
user config leak in through the upward walk.

## Local GitLab

A disposable GitLab CE via docker compose. Needs 4 GB for Docker.

```bash
cp .env.example .env          # set GITLAB_ROOT_PASSWORD
make up                       # start
make wait                     # until healthy; first boot 5-10 min
make install
scripts/seed.py               # PAT + boards/demo.yaml applied
make show PROJECT=root/demo
```

| | |
|---|---|
| Web | http://localhost:8929, user `root`, password from `.env` |
| SSH | port 2224 |
| Stop / wipe | `make down` / `make reset` |

`scripts/seed.py` mints a fixed token (`glpat-seedseed...`, throwaway,
bound to localhost) and applies `boards/demo.yaml` through the CLI. Re-running
it reverts manual board edits to the YAML. `scripts/bulk_demo.py` writes
five `boards/demo-*.yaml` stress boards (gitignored, the script is the
source) and applies them; deterministic per `--seed`. Both scripts are
stdlib-only python3 that shell out to the CLI.

## Layout

| File | Purpose |
|---|---|
| `src/gitboard/cli.py` | Typer CLI, the only entry point |
| `src/gitboard/board.py` | reading; `board_columns` is the gap no MCP server fills |
| `src/gitboard/apply.py` | the only writer; spec schema, `pull`'s read direction |
| `src/gitboard/report.py` | snapshot diffing, commit correlation; local only |
| `src/gitboard/client.py` | connection; API errors become one English line |
| `src/gitboard/config.py` | config singleton, lazy token resolution |
| `src/gitboard/log.py` | stdout for data, stderr for chatter |
