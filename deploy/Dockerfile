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
# UV_PYTHON_PREFERENCE pins the interpreter source. uv's default preference is
# `managed`; today it picks the system python, but a uv-managed one would live
# under /root/.local/share/uv/python, which the runtime stage never copies --
# leaving /opt/cswap/bin/python a dangling symlink that only fails at run time.
ENV UV_PROJECT_ENVIRONMENT=/opt/cswap \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_PREFERENCE=only-system

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

# CA certificates matter enormously here — claude-swap reaches
# api.anthropic.com over TLS, and without a bundle every usage fetch fails as
# CERTIFICATE_VERIFY_FAILED, which cswap reports as a generic "network" error;
# the store then serves stale usage and the engine switches late. (Measured on
# a base that lacked them: usage aged to ~15 min, failures 2/hour -> 31/hour.)
# python:3.12-slim already ships ca-certificates, so no apt layer is needed;
# this comment stays as the reason NOT to move to a barer base.

COPY --from=builder /opt/cswap /opt/cswap

# The official python:*-slim images ship the stdlib with every .pyc deleted, and
# UV_COMPILE_BYTECODE only covers /opt/cswap. Combined with `read_only: true`
# that means each process recompiles ~118 stdlib modules and cannot cache them:
# measured 363-443ms per invocation read-only vs ~100ms once warm. At a 15s
# tick that is ~28 min/day of avoidable CPU, and a 4x slower host shim.
RUN python -m compileall -q /usr/local/lib/python3.12 >/dev/null 2>&1 || true
ENV PATH="/opt/cswap/bin:${PATH}"

COPY deploy/docker-entrypoint.sh deploy/autoswitch-loop.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/autoswitch-loop.sh

# No USER: compose passes the host's uid:gid so anything written into the
# mounted home keeps its host owner and 0600 mode.

EXPOSE 8787
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["cswap", "web", "--bind", "0.0.0.0", "--port", "8787", "--no-browser"]
