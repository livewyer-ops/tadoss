FROM python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
COPY --from=ghcr.io/astral-sh/uv:0.9.25@sha256:13e233d08517abdafac4ead26c16d881cd77504a2c40c38c905cf3a0d70131a6 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    TADOSS_HOST=0.0.0.0
COPY src/pyproject.toml src/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra otel --python /usr/local/bin/python
COPY README.md LICENSE NOTICE ./
COPY src/app ./app
RUN uv sync --frozen --no-dev --extra otel --python /usr/local/bin/python
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('TADOSS_PORT', '8000')}/readyz\", timeout=2)"
CMD ["tadoss"]
