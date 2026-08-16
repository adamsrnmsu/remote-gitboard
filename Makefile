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

.PHONY: help up wait down reset logs seed show plan apply test fmt lint sync repair clean

help:
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/'

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
