# TutorLM instance application image (build context: repository root).
#
#   docker build -f instance-app/deploy/Dockerfile -t tutorlm-host:<tag> .
#
# Paths below are repository-root relative, matching that command. To have
# instance-app/deploy/.dockerignore honored, build with BuildKit and place it as
# deploy/Dockerfile.dockerignore, or copy it to the context root as .dockerignore.
#
# This repository performs no Docker build, pull or push: the file is kept valid so
# the outer team can build it later. Architecture and hardening design live in
# ../deploy/README.md and docs/instance-app-design.md.
#
# Boundaries respected by construction:
#   * only the in-container application is packaged (backend + static frontend);
#   * no Docker socket, no host port publishing, no outer configuration;
#   * no TeX toolchain and no compiler: compilation belongs to a separate trusted
#     executor that the application only submits declarative tasks to.
#   * the runtime user is the non-root uid/gid 10001, matching the owner the outer
#     volume-copy helper hands the persistent data root to.

FROM node:22-slim AS frontend
WORKDIR /frontend
COPY instance-app/frontend/package.json instance-app/frontend/package-lock.json ./
RUN npm ci --omit=dev
COPY instance-app/frontend/assets ./assets
COPY instance-app/frontend/scripts ./scripts
RUN npm run build

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# Non-root runtime identity. Fixed to 10001 so volume ownership stays predictable.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app

# The application package ``backend`` is imported as ``backend.*`` (see
# backend/pyproject.toml), so the parent directory must be both the working
# directory and on sys.path (PYTHONPATH above).
WORKDIR /app

# Backend first (application code), then the runtime dependencies. Requirements
# are installed only when the file is actually present so an early tree still
# builds; requirements.txt declares the HTTP stack (fastapi/starlette/pydantic/
# uvicorn) the Host actually imports.
COPY instance-app/backend /app/backend
RUN if [ -f /app/backend/requirements.txt ]; then \
        python -m pip install --no-cache-dir -r /app/backend/requirements.txt; \
    fi

# Exportable frontend modules and self-hosted PDF.js; no developer node_modules.
COPY --from=frontend /frontend/dist /app/static

# Writable locations: the persistent data root on the mounted volume and the
# volume-external application session directory (backed by a tmpfs at runtime).
RUN mkdir -p /data /tmp/tutorlm-session \
    && chown -R 10001:10001 /data /tmp/tutorlm-session

USER 10001:10001

# Reachable only on the container network; the outer control plane never publishes
# a host port and the application never binds one itself.
EXPOSE 8000

STOPSIGNAL SIGTERM

# Readiness uses /health/ready (not /health) per docs/instance-app-design.md section 8.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/ready',timeout=2).status==200 else 1)"

# Entry point serves the assembled ASGI app with uvicorn, the same runtime the
# package runner (backend/__main__.py) intends: ``create_app()`` on 0.0.0.0:8000.
#
# The package runner and this entrypoint use the same application factory.
ENTRYPOINT ["python", "-m", "uvicorn", "backend.adapters.http.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
