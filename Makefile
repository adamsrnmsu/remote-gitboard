# Local GitLab CE lifecycle + the gitboard CLI.
# `uv run gitboard` resolves the entry point from pyproject.toml and syncs the
# environment first, so there is no install step and no venv to activate.
#
# PYTHONPATH=src is belt-and-braces: uv's editable install writes a .pth that
# this machine intermittently stops honouring, leaving `import gitboard`
# failing until `uv sync --reinstall-package gitboard`. Naming src directly
# makes every target deterministic. Harmless when the install is healthy.

PROJECT    ?=
SPEC       ?=
GITBOARD    = PYTHONPATH=src uv run gitboard

.PHONY: help uv up wait down reset logs seed show plan apply test fmt lint sync repair clean

help:
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

uv:  ## install uv if it is missing, then explain how it is used here
	@if command -v uv >/dev/null 2>&1; then \
	  echo "uv is already installed — $$(uv --version)"; \
	elif command -v brew >/dev/null 2>&1; then \
	  echo "uv not found — installing with Homebrew"; brew install uv; \
	else \
	  echo "uv not found — installing with the official script"; \
	  echo "  (https://astral.sh/uv/install.sh — installs to ~/.local/bin, no sudo)"; \
	  curl -LsSf https://astral.sh/uv/install.sh | sh; \
	fi
	@command -v uv >/dev/null 2>&1 || { \
	  echo ""; \
	  echo "uv installed but not on PATH yet."; \
	  echo 'Open a new shell, or: export PATH="$$HOME/.local/bin:$$PATH"'; \
	  exit 1; }
	@printf '%s\n' \
	  "" \
	  "uv replaces pip + venv + pipx. Nothing here is ever pip-installed." \
	  "" \
	  "  Run this project" \
	  "    uv tool install .        \`gitboard\` on your PATH; re-run after code changes" \
	  "    uv run gitboard show     run from the repo without installing" \
	  "    make show                same, but immune to the .pth issue (see: make repair)" \
	  "" \
	  "  Environment" \
	  "    uv sync                  make .venv match pyproject.toml + uv.lock" \
	  "    uv add <pkg>             add a dependency and update uv.lock" \
	  "    uv remove <pkg>          drop one" \
	  "    uv lock --upgrade        refresh pinned versions" \
	  "" \
	  "  Tools you do not want as dependencies" \
	  "    uvx ruff check .         run a tool in a throwaway cached env" \
	  "" \
	  "uv owns .venv in this directory — you never activate it; \`uv run\` and" \
	  "\`make\` use it for you. uv.lock is committed, so the environment is" \
	  "reproducible. Point your editor at .venv/bin/python for autocomplete." \
	  ""

up: .env  ## start the local GitLab container
	docker compose up -d

wait:  ## block until the container reports healthy (cold boot ~3-10 min)
	@until [ "$$(docker inspect -f '{{.State.Health.Status}}' gitlab 2>/dev/null)" = healthy ]; do \
	  docker inspect -f '{{.State.Running}}' gitlab >/dev/null 2>&1 || { echo "gitlab is not running — make up"; exit 1; }; \
	  printf '.'; sleep 10; \
	done; echo " healthy"

down:  ## stop the container, keep the data
	docker compose down

reset:  ## stop and wipe all three volumes
	docker compose down -v

logs:  ## follow container logs
	docker compose logs -f gitlab

seed: wait  ## mint a PAT and apply boards/demo.yaml
	scripts/seed.py

show:  ## print a board: make show PROJECT=group/project
	$(GITBOARD) show $(PROJECT)

plan:  ## preview YAML changes: make plan SPEC=boards/test.yaml
	$(GITBOARD) plan $(SPEC)

apply:  ## write the YAML to GitLab: make apply SPEC=boards/test.yaml
	$(GITBOARD) apply $(SPEC)

test:  ## run the test suite
	PYTHONPATH=src uv run pytest -q

fmt:  ## format (ruff format is black, same style)
	uvx ruff format .

lint:  ## lint, --fix to apply the safe fixes
	uvx ruff check .

sync:  ## install the project and its deps into .venv
	uv sync

repair:  ## fix `ModuleNotFoundError: gitboard` from a stale editable install
	uv sync --reinstall-package gitboard

.env:
	@echo "no .env — cp .env.example .env and set GITLAB_ROOT_PASSWORD" >&2; exit 1

clean:  ## remove venv, caches, and bytecode
	rm -rf .venv .pytest_cache .ruff_cache
	find . -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
