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
	@$(MAKE) --no-print-directory install-hooks
	@echo "Environment ready ($(VENV))"

.PHONY: install-hooks
install-hooks:
	@if [ -d .git ]; then \
	  mkdir -p .git/hooks && cp .githooks/pre-commit .git/hooks/pre-commit \
	    && chmod +x .git/hooks/pre-commit && echo "Installed git hooks"; \
	else echo "not a git checkout; skipping hooks"; fi

.PHONY: help
help:
	@echo "Available commands:"
	@echo "  make setup           - create .venv and install deps (uv sync); copy .env-example if present; install git hooks"
	@echo "  make install-hooks   - install .githooks/pre-commit into .git/hooks"
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
	@echo "  make rls-check       - fail on a non-LOCAL SET app.tenant_id in src/ or migrations/ (RLS-6, mirrors CI)"
	@echo "  make extract-schemas - derive schemas/events/*.json from the event contracts"
	@echo "  make swag            - extract-schemas + regenerate docs/swagger/* and api/asyncapi.{yaml,html}"
	@echo "  make swag-check      - fail if the generated specs or event schemas are stale (drift gate)"
	@echo "  make run             - run the API locally (uvicorn)"
	@echo "  make sweeps          - run the sweeps runner locally"
	@echo "  make migrate         - apply corpus + generation migrations (needs *_MIGRATION_DATABASE_URL)"
	@echo "  make cover           - coverage HTML report"
	@echo "  make cover-func      - coverage summary by file"
	@echo "  make cover-check     - test-ci, then enforce the coverage gate (COVERAGE_THRESHOLD, default 95)"
	@echo "  make build           - build the sdist and wheel (uv build) into dist/"
	@echo "  make ci              - lock-check + fmt-check + lint + typecheck + rls-check + swag-check + test-ci + build"
	@echo "  make docker-up       - start local infra: postgres x2 DBs + pgbouncer + valkey + qdrant + floci + floci-ui (http://localhost:4501)"
	@echo "  make gateway-up      - start the model gateway (Agent Router) + a model stub (opt-in, :1975)"
	@echo "  make gateway-down    - stop them"
	@echo "  make test-gateway    - our client against the gateway: contract tests (needs gateway-up)"
	@echo "  make docker-down     - stop local containers"
	@echo "  make vuln-check      - pip-audit on the locked deps"
	@echo "  make clean           - remove build artefacts"
	@echo ""
	@echo "Schema governance (platform-schemagov 0.4):"
	@echo "  make schema-pull      - pull the schema-gov Docker image"
	@echo "  make schema-validate  - extract-schemas, then validate $(EVENT_SCHEMA_DIR) (no AWS needed)"
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

# RLS-6: the tenant GUC may only be bound with SET LOCAL / set_config(..., true) inside a
# transaction; a session-scoped SET leaks across pooled PgBouncer backends. CI calls this target.
.PHONY: rls-check
rls-check:
	@hits=$$(grep -REn '\bSET[[:space:]]+("?app\.tenant_id"?)[[:space:]]*=' \
	    --include='*.py' --include='*.sql' src/ migrations/ | grep -v 'SET LOCAL' | grep -v 'set_config' || true); \
	if [ -n "$$hits" ]; then \
	  echo "Non-LOCAL SET app.tenant_id detected (RLS-6). Use SET LOCAL inside a transaction."; \
	  echo "$$hits"; exit 1; \
	fi; \
	echo "RLS-6 non-LOCAL SET check passed."

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
	$(PY) -m pytest -m "not e2e and not gateway" --cov=src/rag --cov-report=xml:coverage.xml --cov-report=term

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

# The model gateway (Agent Router) in front of a model stub: opt-in, for local runs of the
# routes that call a model and for the gateway contract tests. Not started by docker-up.
.PHONY: gateway-up
gateway-up:
	docker compose --profile gateway up -d --build model-stub agent-router
	@echo "gateway API on http://localhost:1975 (set GATEWAY_BASE_URL to use it)"
	@echo "try all seven task classes in the browser: http://localhost:8090/console"

.PHONY: gateway-down
gateway-down:
	docker compose --profile gateway stop agent-router model-stub

.PHONY: test-gateway
test-gateway:  ## our client against the real gateway image and the model stub (make gateway-up first)
	$(PY) -m pytest -m gateway tests/gateway

.PHONY: docker-down
docker-down:
	@echo "Stopping local containers..."
	docker compose down

# -----------------------------
# CI
# -----------------------------

.PHONY: ci
ci: lock-check fmt-check lint typecheck rls-check swag-check test-ci build

# -----------------------------
# BUILD
# -----------------------------

.PHONY: build
build:
	$(UV) build --out-dir dist

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

.PHONY: cover-check
cover-check: test-ci
	PY=$(PY) bash .github/scripts/coverage-gate.sh

# -----------------------------
# SCHEMA GOVERNANCE
# -----------------------------

.PHONY: extract-schemas
extract-schemas:
	@echo "Deriving event schemas from the contracts..."
	$(PY) -m rag.infrastructure.events.schemas

.PHONY: schema-pull
schema-pull:
	docker pull --platform "$(SCHEMA_GOV_PLATFORM)" "$(SCHEMA_GOV_IMAGE)"

.PHONY: schema-validate
schema-validate: extract-schemas
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
# OpenAPI (docs/swagger/swagger.{json,yaml}) comes from the FastAPI app; AsyncAPI (api/asyncapi.{yaml,html})
# and the JSON Schemas (schemas/events/) from the event contracts. All generated and committed — never hand-edited.
# Served at: / (landing), /swagger (Swagger UI), /swagger/doc.json|yaml, /asyncapi (events),
# /asyncapi.yaml; FastAPI's own /docs and /openapi.json stay too.
# Run `make swag` after changing a route, a model or an event payload; tests/unit fails on drift.

.PHONY: swag
swag: extract-schemas
	@echo "Generating OpenAPI + AsyncAPI..."
	$(PY) scripts/gen_api_docs.py

.PHONY: swag-check
swag-check:
	$(PY) scripts/gen_api_docs.py --check
	@$(PY) -c "import sys; from rag.infrastructure.events import schemas as s; \
	stale = [n for n, text in s.schema_files().items() if not (s.SCHEMA_DIR / n).is_file() or (s.SCHEMA_DIR / n).read_text(encoding='utf-8') != text]; \
	print('event schemas: stale ' + ', '.join(stale) if stale else 'event schemas: up to date'); sys.exit(1 if stale else 0)"

.PHONY: docs-serve
docs-serve: run

# -----------------------------
# CLEAN
# -----------------------------

.PHONY: clean
clean:
	rm -rf htmlcov dist .pytest_cache .mypy_cache .ruff_cache
	rm -f coverage.xml .coverage
