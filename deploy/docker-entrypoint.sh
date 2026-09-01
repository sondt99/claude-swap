#!/bin/sh
# Put the host's claude-swap venv on PATH, then run whatever the service asked
# for. The image ships no Python and no claude-swap of its own: both come from
# the uv tool venv in the mounted home, so the container and the host `cswap`
# are always literally the same install and cannot drift apart.
set -eu

VENV="${HOME}/.local/share/uv/tools/claude-swap/bin"

if [ ! -x "${VENV}/python" ]; then
  echo "cswap-web: no claude-swap venv at ${VENV}" >&2
  echo "  HOME is '${HOME}' inside the container; it must be the host home" >&2
  echo "  directory that is bind-mounted at its identical path. Check" >&2
  echo "  HOST_HOME in .env, and that 'uv tool install claude-swap' has run." >&2
  exit 1
fi

# The venv's bin/python is a symlink to a uv-managed CPython elsewhere under
# the same home, so the mount covers it too — but a broken symlink here means
# uv moved the interpreter, and the error is worth naming.
if ! "${VENV}/python" -c 'import claude_swap' 2>/dev/null; then
  echo "cswap-web: ${VENV}/python cannot import claude_swap" >&2
  echo "  (uv may have replaced the interpreter — try 'uv tool install --force claude-swap')" >&2
  exit 1
fi

# Importability is not what the CMDs actually need. Both invoke the `cswap`
# console script, and a venv where that is missing, unexecutable, or too old to
# know the subcommand passed the import check and then failed on every tick --
# silently, because the loop swallowed it. Fail here instead.
if [ ! -x "${VENV}/cswap" ]; then
  echo "cswap-web: no executable cswap at ${VENV}/cswap" >&2
  echo "  the venv imports claude_swap but has no console script; reinstall it" >&2
  exit 1
fi
if ! "${VENV}/cswap" --version >/dev/null 2>&1; then
  echo "cswap-web: ${VENV}/cswap is present but does not run" >&2
  exit 1
fi

PATH="${VENV}:${PATH}"
export PATH

exec "$@"
