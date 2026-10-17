# syntax=docker/dockerfile:1
# AIC Phase 1 container image — PREPARED, NEVER BUILT OR RUN in the Phase 1
# sandbox (Docker unavailable). See docs/DEPLOYMENT.md §9 and docs/TESTING.md §5.
#
# Properties (spec §17 / §23):
# - python:3.12-slim maintained base, minimal runtime dependencies.
# - Non-root execution (appuser, no shell-less hardening beyond non-root).
# - No secrets baked into image layers: ALL secrets (AIC_MASTER_KEYS,
#   AIC_DATABASE_URL, tokens) are injected via environment at runtime.
# - HEALTHCHECK uses python's urllib (curl is not present in slim).
# - psycopg[binary] is installed for the PostgreSQL production target
#   (the Phase 1 sandbox verified SQLite only — see ADR-0002).

FROM python:3.14-slim

# Prevent Python from writing .pyc/buffered output in containers.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Non-root user (spec §23 "Run as a non-root user").
RUN groupadd --system --gid 1001 appuser \
    && useradd --system --uid 1001 --gid appuser --create-home appuser

WORKDIR /app

# Install the pinned application (pyproject.toml) + the PostgreSQL driver.
# Layer ordering: dependency install is its own layer for cache reuse.
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install --no-cache-dir . "psycopg[binary]==3.2.13"

# Operational files: migrations, CLI scripts, alembic config.
COPY alembic.ini ./
COPY migrations/ ./migrations/
COPY scripts/ ./scripts/

# Application data directory (SQLite dev) — owned by appuser, volume-mountable.
RUN mkdir -p /app/data && chown -R appuser:appuser /app
USER appuser

# API port (default 8010; Railway injects PORT — see docs/DEPLOYMENT.md).
EXPOSE 8010

# Liveness probe: /health is unauthenticated and rate-limit-exempt.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os,urllib.request,sys; port=os.environ.get('PORT','8010'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=4).status==200 else 1)"

# API process. The worker runs from the same image with a different command
# (see compose.yaml: `python scripts/run_worker.py --loop`), and the migrate
# step with `alembic upgrade head`.
CMD ["sh", "-c", "uvicorn commons_core.api.app:app --host 0.0.0.0 --port ${PORT:-8010}"]
