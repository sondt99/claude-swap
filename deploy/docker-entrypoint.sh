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

PATH="${VENV}:${PATH}"
export PATH

exec "$@"
