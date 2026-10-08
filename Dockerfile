FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --locked --no-dev

ENV PM_DATA_DIR=/data \
    PM_HOST=0.0.0.0 \
    PM_PORT=8000
VOLUME /data
EXPOSE 8000

CMD ["uv", "run", "--no-sync", "printermonitor"]
