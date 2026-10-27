# -----------------------------
# CONFIG
# -----------------------------
-include .env

APP_NAME      ?= llm-retrieval-generation
APP_ENV       ?= dev
APP_PORT      ?= 8080
VENV          ?= .venv
PY            ?= $(VENV)/bin/python
# uv from PATH, else the copy `make setup` installs into the venv.
UV            ?= $(shell command -v uv 2>/dev/null || echo $(VENV)/bin/uv)
BUILD_VERSION ?= $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)

# Docker socket for testcontainers (Mac Docker Desktop uses a user socket).
DOCKER_SOCKET     := $(shell [ -S /Users/$(USER)/.docker/run/docker.sock ] && echo unix:///Users/$(USER)/.docker/run/docker.sock || echo unix:///var/run/docker.sock)
export DOCKER_HOST ?= $(DOCKER_SOCKET)

export APP_NAME APP_ENV BUILD_VERSION

# Test groups (pytest dirs / markers).
TEST_UNIT_PATHS := tests/unit tests/usecase tests/architecture tests/api tests/events \
                   tests/isolation tests/repository

SCHEMA_GOV_IMAGE ?= ghcr.io/bcbp-solutions-fzc-llc/platform-schemagov:0.4
# Image is published linux/amd64 only; Apple Silicon needs this for pull/run.
SCHEMA_GOV_PLATFORM ?= linux/amd64
EVENT_SCHEMA_DIR ?= schemas/events

# -----------------------------
# SETUP
# -----------------------------

# Defaults point at docker-compose.yml's ports; without .env the app falls back to localhost:5432.
.env:
	@test -f .env-example || { echo "no .env and no .env-example"; exit 1; }
	cp .env-example .env
	@echo "Created .env from .env-example — start the infra with: make docker-up"

.PHONY: setup
setup:
	@test -f .env || { test -f .env-example && cp .env-example .env || true; }
	@command -v uv >/dev/null 2>&1 || test -x $(VENV)/bin/uv || { \
	  test -x $(PY) || python3 -m venv $(VENV); \
	  echo "uv not found — installing into $(VENV)"; \
	  $(PY) -m pip install -q 'uv==0.13.1'; }
	$(UV) sync --locked
	@echo "Environment ready ($(VENV))"

.PHONY: help
help:
	@echo "Available commands:"
	@echo "  make setup           - create .venv and install deps (uv sync); copy .env-example if present"
	@echo "  make lock            - refresh the lockfile (uv lock)"
	@echo "  make lock-check      - fail if uv.lock is stale vs pyproject.toml"
	@echo "  make fmt             - ruff format + autofix"
	@echo "  make fmt-check       - verify ruff formatting (mirrors CI)"
	@echo "  make lint            - ruff check"
	@echo "  make typecheck       - mypy --strict on src"
	@echo "  make test            - unit + usecase + architecture + api (no Docker)"
	@echo "  make test-ci         - unit + integration with coverage (used in CI; requires Docker)"
	@echo "  make test-unit       - unit tests only (no Docker required)"
	@echo "  make test-integration - migration/Postgres integration tests (requires Docker)"
	@echo "  make test-e2e        - end-to-end tests: real app + RLS Postgres (requires Docker)"
	@echo "  make swag            - regenerate docs/swagger/swagger.{json,yaml} + api/asyncapi.yaml"
	@echo "  make swag-check      - fail if the generated specs are stale (drift gate)"
	@echo "  make run             - run the API locally (uvicorn)"
	@echo "  make sweeps          - run the sweeps runner locally"
	@echo "  make migrate         - apply corpus + generation migrations (needs *_MIGRATION_DATABASE_URL)"
	@echo "  make cover           - coverage HTML report"
	@echo "  make cover-func      - coverage summary by file"
	@echo "  make ci              - fmt-check + lint + typecheck + test-ci"
	@echo "  make docker-up       - start local infra: postgres x2 DBs + pgbouncer + valkey + qdrant + floci + floci-ui (http://localhost:4501)"
	@echo "  make docker-down     - stop local containers"
	@echo "  make vuln-check      - pip-audit on the locked deps"
	@echo "  make clean           - remove build artefacts"
	@echo ""
	@echo "Schema governance (platform-schemagov 0.4):"
	@echo "  make schema-pull      - pull the schema-gov Docker image"
	@echo "  make schema-validate  - validate event schemas in $(EVENT_SCHEMA_DIR) (no AWS needed)"
	@echo "  make schema-diff      - diff two schemas: CURRENT=<path> PROPOSED=<path>"

# -----------------------------
# PYTHON BASICS
# -----------------------------

.PHONY: lock
lock:
	$(UV) lock

.PHONY: lock-check
lock-check:
	$(UV) lock --check

.PHONY: fmt
fmt:
	$(PY) -m ruff check --fix src tests
	$(PY) -m ruff format src tests

.PHONY: fmt-check
fmt-check:
	$(PY) -m ruff format --check src tests
	@echo "ruff format: all files formatted"

.PHONY: vuln-check
vuln-check:
	$(UV) run --with pip-audit pip-audit

# -----------------------------
# LINT
# -----------------------------

.PHONY: lint
lint:
	@echo "Running linter..."
	$(PY) -m ruff check src tests
	$(PY) -m ruff format --check src tests

.PHONY: typecheck
typecheck:
	$(PY) -m mypy src

# -----------------------------
# TESTS
# -----------------------------

.PHONY: test
test:  ## unit + architecture + api (no Docker)
	$(PY) -m pytest

.PHONY: test-ci
test-ci:  ## unit + integration, one coverage run (needs Docker)
	$(PY) -m pytest -m "not e2e" --cov=src/rag --cov-report=xml:coverage.xml --cov-report=term

.PHONY: test-unit
test-unit:
	$(PY) -m pytest $(TEST_UNIT_PATHS)

.PHONY: test-integration
test-integration:  ## needs Docker
	$(PY) -m pytest -m integration

.PHONY: test-e2e
test-e2e:  ## real app over real Postgres; needs Docker
	$(PY) -m pytest -m e2e

# -----------------------------
# RUN
# -----------------------------

.PHONY: run
run: .env
	@-lsof -ti :$(APP_PORT) | xargs kill -9 2>/dev/null; true
	bash -c 'set -a && [ -f .env ] && source .env; set +a && BUILD_VERSION=$(BUILD_VERSION) $(PY) -m uvicorn --factory rag.presentation.main:create_app --host 0.0.0.0 --port $(APP_PORT)'

.PHONY: sweeps
sweeps: .env
	bash -c 'set -a && [ -f .env ] && source .env; set +a && $(PY) -m rag.infrastructure.sweeps'

.PHONY: migrate
migrate: .env
	bash -c 'set -a && [ -f .env ] && source .env; set +a && $(PY) -m rag.infrastructure.persistence.migrate'

# -----------------------------
# DOCKER
# -----------------------------

.PHONY: docker-up
docker-up:
	@echo "Starting local infra..."
	docker compose up -d --wait postgres pgbouncer redis qdrant floci floci-ui

# floci-ui only shows queue counts, so read the dev probe queue (fed by both topics) from the CLI.
.PHONY: events-peek
events-peek:
	docker compose exec -e AWS_ACCESS_KEY_ID=test -e AWS_SECRET_ACCESS_KEY=test floci aws --region eu-west-1 \
	  --endpoint-url http://localhost:4566 sqs receive-message --max-number-of-messages 10 \
	  --queue-url http://localhost:4566/000000000000/llm-events-probe-q --visibility-timeout 0 \
	  --query 'Messages[].Body' --output text | tr '\t' '\n'

.PHONY: docker-down
docker-down:
	@echo "Stopping local containers..."
	docker compose down

# -----------------------------
# CI
# -----------------------------

.PHONY: ci
ci: fmt-check lint typecheck test-ci

# kept from the previous Makefile
.PHONY: check
check: lint typecheck test

# -----------------------------
# COVERAGE
# -----------------------------

.PHONY: cover
cover: test-ci
	$(PY) -m coverage html && open htmlcov/index.html

.PHONY: cover-func
cover-func: test-ci
	$(PY) -m coverage report

# -----------------------------
# SCHEMA GOVERNANCE
# -----------------------------

.PHONY: schema-pull
schema-pull:
	docker pull --platform "$(SCHEMA_GOV_PLATFORM)" "$(SCHEMA_GOV_IMAGE)"

.PHONY: schema-validate
schema-validate:
	docker run --rm --platform "$(SCHEMA_GOV_PLATFORM)" \
	  -v "$(CURDIR)":/workspace \
	  "$(SCHEMA_GOV_IMAGE)" validate \
	  --schema-dir $(EVENT_SCHEMA_DIR)

.PHONY: schema-diff
schema-diff:
	@test -n "$(CURRENT)" && test -n "$(PROPOSED)" || { \
	  echo "Usage: make schema-diff CURRENT=<current.json> PROPOSED=<proposed.json>"; \
	  exit 1; \
	}
	docker run --rm --platform "$(SCHEMA_GOV_PLATFORM)" \
	  -v "$(CURDIR)":/workspace \
	  "$(SCHEMA_GOV_IMAGE)" diff \
	  --current     "$(CURRENT)" \
	  --proposed    "$(PROPOSED)" \
	  --schema-name "$(or $(SCHEMA_NAME),$(notdir $(basename $(PROPOSED))))"

# -----------------------------
# DOCS
# -----------------------------
# OpenAPI (docs/swagger/swagger.{json,yaml}) comes from the FastAPI app; AsyncAPI (api/asyncapi.yaml)
# from the event contracts. Both are generated and committed — never hand-edited.
# Served at: / (landing), /swagger (Swagger UI), /swagger/doc.json|yaml, /asyncapi (events),
# /asyncapi.yaml; FastAPI's own /docs and /openapi.json stay too.
# Run `make swag` after changing a route, a model or an event payload; tests/unit fails on drift.

.PHONY: swag
swag:
	@echo "Generating OpenAPI + AsyncAPI..."
	$(PY) scripts/gen_api_docs.py

.PHONY: swag-check
swag-check:
	$(PY) scripts/gen_api_docs.py --check

.PHONY: docs-serve
docs-serve: run

# -----------------------------
# CLEAN
# -----------------------------

.PHONY: clean
clean:
	rm -rf htmlcov .pytest_cache .mypy_cache .ruff_cache
	rm -f coverage.xml .coverage
