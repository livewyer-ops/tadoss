FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.25 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    TADOSS_HOST=0.0.0.0
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python
COPY README.md LICENSE NOTICE ./
COPY tadoss ./tadoss
RUN uv sync --frozen --no-dev --python /usr/local/bin/python
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.getenv('TADOSS_PORT', '8000')}/readyz\", timeout=2)"
CMD ["tadoss"]
