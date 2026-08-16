# Local GitLab CE lifecycle + the gitboard CLI.
#
# Everything here runs the code straight out of src/ via PYTHONPATH, and never
# through an editable install. On this machine a .pth-based editable install
# works for a few seconds after `pip install -e .` (or `uv sync`) and then
# stops — the file is present and readable, its target exists, site lists it,
# and the path still is not added. Reproduced identically with pip and with
# uv, so it is not a uv problem. Root cause unknown.
#
# What is reliable: anything that copies the package (`pip install .`,
# `uv tool install .`) and anything that names src/ directly (PYTHONPATH).
# Both are used below. Do not "simplify" these away.

VENV       = .venv
PY         = $(VENV)/bin/python
PROJECT    ?=
SPEC       ?=
GITBOARD    = PYTHONPATH=src $(PY) -m gitboard.cli

.PHONY: help install uv up wait down reset logs seed show plan apply test fmt lint clean

help:
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

$(VENV)/bin/pytest: pyproject.toml
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q ".[dev]"
	@echo "installed into $(VENV) — no uv required"

install: $(VENV)/bin/pytest  ## create .venv and install (plain venv + pip)

uv:  ## optional: install uv, and explain how it is used here
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
	  "uv is optional here — \`make install\` uses plain venv + pip." \
	  "What uv is good for in this project:" \
	  "" \
	  "    uv tool install .        \`gitboard\` on your PATH, anywhere" \
	  "    uvx ruff check .         run a tool without installing it" \
	  "" \
	  "Avoid \`uv run gitboard\`: it relies on an editable install, which is" \
	  "the one thing that does not work reliably on this machine." \
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

seed: wait install  ## mint a PAT and apply boards/demo.yaml
	scripts/seed.py

show: install  ## print a board: make show PROJECT=group/project
	@$(GITBOARD) show $(PROJECT)

plan: install  ## preview YAML changes: make plan SPEC=boards/test.yaml
	@$(GITBOARD) plan $(SPEC)

apply: install  ## write the YAML to GitLab: make apply SPEC=boards/test.yaml
	@$(GITBOARD) apply $(SPEC)

test: install  ## run the test suite
	PYTHONPATH=src $(VENV)/bin/pytest -q

fmt:  ## format (ruff format is black, same style)
	uvx ruff format .

lint:  ## lint, --fix to apply the safe fixes
	uvx ruff check .

.env:
	@echo "no .env — cp .env.example .env and set GITLAB_ROOT_PASSWORD" >&2; exit 1

clean:  ## remove venv, caches, and bytecode
	rm -rf $(VENV) .pytest_cache .ruff_cache
	find . -name __pycache__ -not -path './$(VENV)/*' -exec rm -rf {} + 2>/dev/null || true
