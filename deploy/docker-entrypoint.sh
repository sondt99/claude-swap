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

PATH="${VENV}:${PATH}"
export PATH

[ "$#" -gt 0 ] || { echo "cswap: entrypoint called with no command" >&2; exit 2; }

exec "$@"
