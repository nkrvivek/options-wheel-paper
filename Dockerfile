# Multi-stage Python build for the paper-wheel Cloudflare Container.
#
# Stage 1: build   — compile wheels for the runtime deps
# Stage 2: runtime — slim image, non-root, healthcheck-ready
#
# Cron-driven: the Worker's scheduled() handler POSTs /run-daily at 14:45 UTC
# Mon-Fri (10:45 ET), which is the schedule the retired GitHub Actions path
# used to receive by workflow_dispatch. Container listens on 8080 (server.py).
#
# Build:  docker build -t options-wheel:latest .
# Local:  docker run --rm --env-file .env -p 8080:8080 options-wheel:latest
# Smoke:  curl -fsS http://localhost:8080/healthz
FROM python:3.12-slim AS build

WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --wheel-dir=/wheels -r requirements.txt

# ---- runtime ----
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/New_York \
    PIP_NO_CACHE_DIR=1

# Non-root user (uid 10001 -> CF Container best practice)
RUN groupadd -g 10001 wheel \
    && useradd -u 10001 -g wheel -m -s /sbin/nologin wheel

# tini for signal handling; tzdata so the market-session date (America/New_York)
# resolves inside the container exactly as it does in run_daily.market_session_date.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini tzdata ca-certificates curl \
    && ln -sf /usr/share/zoneinfo/America/New_York /etc/localtime \
    && echo "America/New_York" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /wheels /wheels
COPY requirements.txt /app/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /app/requirements.txt \
    && rm -rf /wheels

WORKDIR /app

# Cache-bust: bump to force the COPY layer to reroll when only source changed.
ARG CACHE_BUST=20260829-cf-migration
RUN echo "cache-bust: $CACHE_BUST" > /tmp/cache_bust

COPY --chown=wheel:wheel . /app

# run_strategy --log-to-file and the local state fallback both want writable dirs.
RUN mkdir -p /app/logs /app/state && chown -R wheel:wheel /app/logs /app/state

USER wheel

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "server"]
