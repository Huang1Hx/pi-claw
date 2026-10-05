FROM ghcr.io/astral-sh/uv:0.12.10@sha256:2bb3ebca0a796a155094a27773d290c4b074572e6107f171d88d086682fd2500 AS uv
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

COPY --from=uv /uv /uvx /usr/local/bin/

ENV DEBIAN_FRONTEND=noninteractive \
    UV_PROJECT_ENVIRONMENT=/opt/venv

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --all-groups --no-install-project \
    && rm -rf /root/.cache/uv /build \
    && mkdir -p /input /workspace \
    && chmod 0555 /input \
    && chmod 0700 /workspace

COPY docker/sandbox/entrypoint.sh /usr/local/bin/pi-claw-sandbox-entrypoint
RUN chmod 0555 /usr/local/bin/pi-claw-sandbox-entrypoint

ENV HOME=/tmp \
    PATH=/opt/venv/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/workspace/src \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_OFFLINE=1 \
    UV_NO_SYNC=1 \
    UV_NO_ENV_FILE=1

WORKDIR /workspace
USER 65532:65532
ENTRYPOINT ["/usr/local/bin/pi-claw-sandbox-entrypoint"]
