#!/bin/sh
# Autoswitch tick loop.
#
# Replaces `cswap auto`'s own loop with `cswap auto --once` on a timer, so
# settings.json is re-read on every tick: the built-in loop reads it once at
# startup, which meant `cswap config set ...` needed a container restart.
#
# --once is the supported shape for this: cooldown and quarantine persist in
# autoswitch_state.json precisely so cron-driven ticks behave across processes,
# and the exit code reports the outcome.
#
# Pausing is NOT handled here. `cswap auto --once` consults the flag itself, so
# a second implementation in shell would only be a path that could drift out of
# step with the engine's — which is exactly the split that made the dashboard's
# pause button a no-op for a native `cswap auto`.
set -u

TICK_S="${CSWAP_TICK_S:-15}"
# A non-numeric value makes `sleep` fail instantly, turning the loop into a
# fork bomb that spawns `cswap auto --once` hundreds of times a second against
# the credential store. Measured at ~574 iterations/sec before this guard.
case "${TICK_S}" in
    ''|*[!0-9]*) echo "invalid CSWAP_TICK_S='${TICK_S}', using 15" >&2; TICK_S=15 ;;
esac

# Consecutive hard failures before giving up. `|| true` on its own made every
# failure indistinguishable from success: a loop whose command was missing or
# incompatible span forever while the container reported Up, restart policies
# never fired, and `docker top` still showed a process.
MAX_FAILS="${CSWAP_MAX_FAILS:-20}"

# Touched after every tick that actually evaluated. The healthcheck reads its
# mtime, so "the process exists" is no longer mistaken for "the engine works".
HEARTBEAT="${XDG_DATA_HOME:-${HOME}/.local/share}/claude-swap/.autoloop-heartbeat"
mkdir -p "$(dirname "${HEARTBEAT}")" 2>/dev/null || true

# `init: true` in compose handles this too, but a trap keeps a bare
# `docker run` of this image well-behaved as well.
trap 'exit 0' TERM INT

echo "autoswitch loop: every ${TICK_S}s, heartbeat ${HEARTBEAT}"

fails=0
while true; do
    cswap auto --once
    rc=$?
    case "${rc}" in
        # Every outcome the engine defines: switched / nothing to do / blocked
        # with no viable target / deliberately paused. All mean it ran.
        0|2|3|4)
            fails=0
            touch "${HEARTBEAT}" 2>/dev/null || true
            ;;
        # 1 is the engine's own ERROR — a transient network or lock problem is
        # expected here and must not kill the loop, but a permanent one should
        # not masquerade as health either.
        *)
            fails=$((fails + 1))
            echo "$(date +%H:%M:%S)  tick failed (exit ${rc}), ${fails}/${MAX_FAILS} consecutive" >&2
            if [ "${fails}" -ge "${MAX_FAILS}" ]; then
                echo "$(date +%H:%M:%S)  giving up after ${fails} consecutive failures — exiting so the restart policy can act" >&2
                exit 1
            fi
            ;;
    esac
    sleep "${TICK_S}"
done
