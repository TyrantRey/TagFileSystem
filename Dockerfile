# Code by AkinoAlice@TyrantRey
#
# The TagFileSystem daemon as one container (DESIGN/v0-5-0.md §10): the web UI
# built in a Node stage, the Python package installed from uv.lock into a
# virtual environment, no git checkout inside. The managed root is the
# volume at /data; the control channel and the web UI are port 7411.
#
#   docker compose up -d --build                                     # docker-compose.yml
#   docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) -t tagfilesystem .

# ---- the web UI -------------------------------------------------------------
FROM node:22-alpine AS ui
WORKDIR /ui
COPY Frontend/package.json Frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY Frontend/ ./
RUN npm run build

# ---- the daemon -------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime
COPY --from=ghcr.io/astral-sh/uv:0.10.7 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH=/app/.venv/bin:$PATH \
    TFS_UI_DIR=/app/ui \
    TFS_REGISTRY=/tmp/tfs/roots.json

WORKDIR /app

# Dependencies first: this layer changes only when the lock does.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project

# The project itself, installed rather than editable: the image is not a
# checkout, so version.py reads the installed metadata and TFS_COMMIT.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Packages the add-ons in the root's script/ import (see the file).
COPY docker/requirements-addons.txt ./docker/
RUN --mount=type=cache,target=/root/.cache/uv \
    if grep -qvE '^[[:space:]]*(#|$)' docker/requirements-addons.txt; then \
        uv pip install --requirements docker/requirements-addons.txt; \
    fi

COPY --from=ui /ui/dist ./ui
COPY docker/entrypoint.sh docker/healthcheck.py ./docker/

ARG GIT_COMMIT=""
ENV TFS_COMMIT=$GIT_COMMIT
LABEL org.opencontainers.image.title="TagFileSystem" \
      org.opencontainers.image.description="Tags in file names, add-on functions in per-folder YAML, with a queryable SQLite log" \
      org.opencontainers.image.source="https://github.com/TyrantRey/TagFileSystem" \
      org.opencontainers.image.revision=$GIT_COMMIT

# A non-root user by default; docker-compose.yml overrides the uid:gid so
# the files the daemon creates in the root belong to whoever owns it.
RUN groupadd --gid 1000 tfs \
    && useradd --uid 1000 --gid tfs --create-home tfs \
    && sed -i 's/\r$//' docker/entrypoint.sh \
    && chmod 755 docker/entrypoint.sh \
    && mkdir -p /data \
    && chown tfs:tfs /data
USER tfs
WORKDIR /data
EXPOSE 7411
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]
ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["tfs", "start", "--log-console"]
