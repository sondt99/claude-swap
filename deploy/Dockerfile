# Self-contained: this image builds claude-swap from the source tree beside it
# and ships its own interpreter.
#
# It used to carry neither, borrowing the host's `uv tool` venv through the
# mounted home. That kept one install on the box, but it made the containers
# depend on a host-side `uv tool install` and meant `docker compose up` could
# silently run whatever version happened to be installed there. Building from
# source is what makes the image reproducible and the host free of any cswap
# install at all.
#
# The credential store still lives on the host and is still bind-mounted — only
# the *code* moved into the image.
#
# Build context is the REPOSITORY ROOT, not deploy/ (see docker-compose.yml),
# because the build needs pyproject.toml, uv.lock and src/.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

# Build the venv at its FINAL path. A venv records absolute paths in its
# scripts' shebangs, so building at /src/.venv and copying to /opt would leave
# every console script pointing at an interpreter that does not exist.
ENV UV_PROJECT_ENVIRONMENT=/opt/cswap \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /src
# Dependency layer first: it changes far less often than the source, so editing
# a Python file does not re-resolve the whole lock.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --no-dev

COPY src ./src
# --no-editable: an editable install would point into /src, which does not
# exist in the runtime stage.
RUN uv sync --locked --no-dev --no-editable


FROM python:3.12-slim

# ca-certificates is NOT optional. claude-swap reaches api.anthropic.com over
# TLS to read usage, and a base image without a CA bundle fails every fetch
# with CERTIFICATE_VERIFY_FAILED. cswap reports that as a generic "network"
# error, so the store just backs off and keeps serving stale usage — the
# engine then decides on data that is many minutes old and switches late.
# Measured when this was missing: usage aged to ~15 minutes and fetch failures
# went from ~2/hour to ~31/hour.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/cswap /opt/cswap
ENV PATH="/opt/cswap/bin:${PATH}"

COPY deploy/docker-entrypoint.sh deploy/autoswitch-loop.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/autoswitch-loop.sh

# No USER: compose passes the host's uid:gid so anything written into the
# mounted home keeps its host owner and 0600 mode.

EXPOSE 8787
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["cswap", "web", "--bind", "0.0.0.0", "--port", "8787", "--no-browser"]
