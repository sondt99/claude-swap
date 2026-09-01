#!/bin/sh
# claude-swap is installed in this image at /opt/cswap, built from the source
# tree at image-build time. Nothing is taken from the host any more except the
# credential store itself, which is bind-mounted.
set -eu

VENV="/opt/cswap/bin"

if [ ! -x "${VENV}/cswap" ]; then
  echo "cswap: no executable cswap at ${VENV}/cswap" >&2
  echo "  the image did not build correctly; rebuild with --build" >&2
  exit 1
fi

# Importability is not what the CMDs need — both invoke the console script, and
# a venv that imports the package but has no working script would otherwise
# fail on every tick, silently, because the loop swallows it.
if ! "${VENV}/cswap" --version >/dev/null 2>&1; then
  echo "cswap: ${VENV}/cswap is present but does not run" >&2
  exit 1
fi

# HOME must be the bind-mounted host home: that is where the credential store,
# settings.json and ~/.claude.json live. Without it cswap would happily operate
# on an empty store inside the container and look like it had no accounts.
if [ ! -d "${HOME}" ]; then
  echo "cswap: HOME='${HOME}' does not exist inside the container" >&2
  echo "  it must be the host home directory, bind-mounted at the same path." >&2
  echo "  Check HOST_HOME in deploy/.env." >&2
  exit 1
fi

# The directory check alone does not catch the failure it was written for: a
# typo in HOST_HOME makes Docker AUTO-CREATE the bind source, so an empty
# root-owned directory mounts cleanly, the check passes, and cswap reports "no
# accounts are managed yet" from a green, healthy container. Look for the store
# itself instead.
STORE="${XDG_DATA_HOME:-${HOME}/.local/share}/claude-swap"
if [ ! -d "${STORE}" ] && [ ! -d "${HOME}/.claude" ] && [ ! -f "${HOME}/.claude.json" ]; then
  echo "cswap: '${HOME}' is mounted but contains no Claude state." >&2
  echo "  Expected one of:" >&2
  echo "    ${STORE}" >&2
  echo "    ${HOME}/.claude" >&2
  echo "    ${HOME}/.claude.json" >&2
  echo "  A typo in HOST_HOME makes Docker create an empty directory and mount" >&2
  echo "  that, which looks healthy and serves an empty dashboard. Refusing." >&2
  exit 1
fi

PATH="${VENV}:${PATH}"
export PATH

[ "$#" -gt 0 ] || { echo "cswap: entrypoint called with no command" >&2; exit 2; }

exec "$@"
