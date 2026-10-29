# Pin base images to SHA digests for reproducible, supply-chain-safe builds.
# Update with: make pin-base-images  (requires docker and network access)
# Current digests are tracked in .docker-digests; CI (validate-quality.yml,
# "Dockerfile base image digest check") fails when the two disagree: a bump to
# one (e.g. a Dependabot PR) must update the other.
FROM ghcr.io/astral-sh/uv:0.13.6@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.15.2-slim-bookworm@sha256:4e03260adeeebafccd084f3584e6b84823aba5651cfb53ef8a078ed441ddf9cd AS builder

COPY --from=uv /uv /usr/local/bin/uv

# A C toolchain for the build stage only: asyncpg 0.31 (pinned below 0.32 by pgcommon 0.4.1) has no
# wheel for Python 3.15, so it is compiled here. The runtime stage has no compiler. Drop this when
# pgcommon allows asyncpg 0.32, which ships 3.15 wheels.
# hadolint ignore=DL3008
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libc6-dev && \
    rm -rf /var/lib/apt/lists/*

# Bytecode is compiled once here; the runtime image has no compiler and a read-only code tree.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first (a layer that changes only with uv.lock). The platform libraries
# (platform-fastapicommon, pgcommon, eventcommon) resolve from the registry recorded in uv.lock.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# The package is installed editable at /app/src: the code finds migrations/, schemas/, api/ and
# docs/swagger/ relative to its own location, so the runtime image keeps this layout.
COPY src ./src
RUN uv sync --locked --no-dev

FROM python:3.15.2-slim-bookworm@sha256:4e03260adeeebafccd084f3584e6b84823aba5651cfb53ef8a078ed441ddf9cd

ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION} \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

LABEL org.opencontainers.image.title="llm-retrieval-generation" \
      org.opencontainers.image.source="https://github.com/BCBP-SOLUTIONS-FZC-LLC/llm-retrieval-generation" \
      org.opencontainers.image.vendor="BCBP Solutions FZC LLC" \
      org.opencontainers.image.revision="${BUILD_VERSION}"

# Numeric, non-root user so a Kubernetes runAsNonRoot check can verify it.
RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=builder --chown=10001:10001 /app/src /app/src
# Runtime data the code reads: DDL for the init job, the derived event schemas and the generated
# API documents served at /docs and /asyncapi.
COPY --chown=10001:10001 migrations ./migrations
COPY --chown=10001:10001 schemas ./schemas
COPY --chown=10001:10001 api ./api
COPY --chown=10001:10001 docs/swagger ./docs/swagger

USER 10001:10001

EXPOSE 8080

# Liveness only: /ready depends on the databases and the relays, and Kubernetes probes it itself.
# This lets plain `docker run` / compose see a dead process; the chart's probes are authoritative.
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/live', timeout=2)"]

# keep-alive 65 s outlives a load balancer's 60 s idle timeout; graceful shutdown 60 s leaves the chart's
# 160 s grace period room for the relays to drain after connections close.
# The same image runs three entrypoints; the deployment chooses by command:
#   API (default)   uvicorn --factory rag.presentation.main:create_app
#   migrations      python -m rag.infrastructure.persistence.migrate   (init job, one at a time)
#   sweeps          python -m rag.infrastructure.sweeps <name>         (CronJob)
ENTRYPOINT ["uvicorn", "--factory", "rag.presentation.main:create_app", "--host", "0.0.0.0", "--port", "8080", "--timeout-keep-alive", "65", "--timeout-graceful-shutdown", "60"]
