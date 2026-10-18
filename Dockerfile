FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY log_and_order ./log_and_order
COPY main.py ./

RUN uv sync --frozen --no-dev --extra api --no-cache \
    && chown -R app:app /app

USER app

ENV PATH="/app/.venv/bin:${PATH}" \
    LOG_AND_ORDER_HOST=0.0.0.0 \
    LOG_AND_ORDER_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; port=os.environ.get('LOG_AND_ORDER_PORT', '8080'); urllib.request.urlopen(f'http://127.0.0.1:{port}/api/v1/health', timeout=3)"

CMD ["python", "-m", "log_and_order.api"]
