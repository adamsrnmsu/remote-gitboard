# Local GitLab CE lifecycle + the read-only board reader.
# The scripts are `uv run --script` with inline deps — `venv` is for your
# editor's autocomplete, not for running them.

PROJECT    ?= root/demo
GITLAB_URL ?= http://localhost:8929
export GITLAB_URL

.PHONY: help up wait down reset logs seed board test fmt lint venv clean

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

seed: wait  ## mint a PAT and seed the demo board
	./seed.py

board:  ## print a board: make board PROJECT=group/project
	./board.py $(PROJECT)

test:  ## column-bucketing selftest, no deps needed
	./board.py --selftest

fmt:  ## format (ruff format is black, same style)
	uvx ruff format .

lint:  ## lint, --fix to apply the safe fixes
	uvx ruff check .

venv:  ## .venv for editor autocomplete only
	uv venv && uv pip install python-gitlab

.env:
	@echo "no .env — cp .env.example .env and set GITLAB_ROOT_PASSWORD" >&2; exit 1

clean:  ## remove venv and bytecode
	rm -rf .venv __pycache__
