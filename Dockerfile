FROM python:3.13-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    EKORN_CONFIG_PATH=/app/config.example.json \
    EKORN_STATE_PATH=/data/state.json

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 ekorn \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app ekorn \
    && mkdir /data \
    && chown 10001:10001 /data

WORKDIR /app
COPY --chown=10001:10001 care.py configuration.py inline_ui.py migrate.py service.py storage.py telegram_api.py config.example.json ./
USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
CMD ["python3", "service.py"]
