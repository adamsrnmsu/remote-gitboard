# Local GitLab CE lifecycle + the gitboard CLI.
# Everything is `uv run --script` with inline deps — `venv` is for your
# editor's autocomplete, not for running anything.

PROJECT    ?= root/demo
SPEC       ?= boards/demo.yaml
GITLAB_URL ?= http://localhost:8929
export GITLAB_URL

# pytest imports the modules, so it needs their deps. uvx builds this env
# on the fly and caches it; nothing is installed into the repo.
PYTEST = uvx --with pyyaml --with rich --with typer --with python-gitlab pytest

.PHONY: help up wait down reset logs seed show plan apply test fmt lint venv clean

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
	./seed.py

show:  ## print a board: make show PROJECT=group/project
	./gitboard.py show $(PROJECT)

plan:  ## preview YAML changes: make plan SPEC=boards/test.yaml
	./gitboard.py plan $(SPEC)

apply:  ## write the YAML to GitLab: make apply SPEC=boards/test.yaml
	./gitboard.py apply $(SPEC)

test:  ## run the test suite
	$(PYTEST) -q

fmt:  ## format (ruff format is black, same style)
	uvx ruff format .

lint:  ## lint, --fix to apply the safe fixes
	uvx ruff check .

venv:  ## .venv for editor autocomplete only
	uv venv && uv pip install python-gitlab pyyaml typer rich

.env:
	@echo "no .env — cp .env.example .env and set GITLAB_ROOT_PASSWORD" >&2; exit 1

clean:  ## remove venv, caches, and bytecode
	rm -rf .venv __pycache__ tests/__pycache__ .pytest_cache .ruff_cache
