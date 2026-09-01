# Deliberately no Python and no claude-swap in this image.
#
# Both come from the host's uv tool venv, which lives under the home directory
# that compose bind-mounts — so the container runs the *same* claude-swap
# install as your `cswap` CLI. Nothing to pin, nothing to bump in lockstep,
# and no chance of a containerised copy migrating the shared on-disk store to
# a schema the host tool doesn't understand.
#
# The interpreter is uv's portable CPython build (glibc), which runs fine on
# this base. Since the dashboard was vendored into claude-swap itself (it is
# now `cswap web`, shipped inside the wheel), the image no longer carries the
# app code either — all it contributes is the two shell drivers and an
# entrypoint that puts the venv on PATH.
FROM debian:12-slim

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

# server.py and index.html are no longer copied: they live in the wheel now, so
# copying them would create a second, silently-diverging copy of the frontend.
COPY docker-entrypoint.sh autoswitch-loop.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh /usr/local/bin/autoswitch-loop.sh

# No USER: compose passes the host's uid:gid so anything written into the
# mounted home keeps its host owner and 0600 mode.

EXPOSE 8787
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["cswap", "web", "--bind", "0.0.0.0", "--port", "8787", "--no-browser"]
