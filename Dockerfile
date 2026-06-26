# syntax=docker/dockerfile:1

# --- builder: resolve and install dependencies into a venv -----------------
FROM python:3.12-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install third-party deps first, in their own layer: this is cached and only
# re-runs when pyproject.toml / uv.lock change, not on every source edit.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then add the project source and install the package itself.
COPY releasepulse ./releasepulse
RUN uv sync --frozen --no-dev

# --- runtime: copy the venv + source, drop privileges ----------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

RUN useradd --create-home --uid 1000 app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app releasepulse ./releasepulse
COPY --chown=app:app migrations ./migrations
COPY --chown=app:app alembic.ini ./alembic.ini

USER app

EXPOSE 8000

# Default role is the API; the worker and migrate steps override this command.
CMD ["uvicorn", "releasepulse.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
