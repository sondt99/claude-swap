#!/bin/sh
# Autoswitch tick loop with a pause gate.
#
# Replaces `cswap auto`'s own loop with `cswap auto --once` on a timer. Two
# reasons, both things the built-in loop cannot give us:
#
#   1. Pause. The dashboard needs to hold the engine off so a hand-picked
#      account stays active. There is no upstream setting for that, but
#      skipping the tick entirely is equivalent and needs no patching.
#   2. Settings reload. The loop reads settings.json once at startup, so
#      `cswap config set ...` used to need a container restart. A fresh
#      --once process per tick re-reads it every time.
#
# --once is the supported shape for this: cooldown and quarantine persist in
# autoswitch_state.json precisely so cron-driven ticks behave across
# processes, and the exit code reports the outcome.
set -u

PAUSE_FILE="${HOME}/.cswap-web-paused"
TICK_S="${CSWAP_TICK_S:-15}"
# A non-numeric value makes `sleep` fail instantly, turning the loop into a
# fork bomb that spawns `cswap auto --once` hundreds of times a second against
# the credential store. Measured at ~574 iterations/sec before this guard.
case "${TICK_S}" in
    ''|*[!0-9]*) echo "invalid CSWAP_TICK_S='${TICK_S}', using 15" >&2; TICK_S=15 ;;
esac

echo "autoswitch loop: every ${TICK_S}s, pause file ${PAUSE_FILE}"

# `init: true` in compose handles this too, but a trap keeps a bare
# `docker run` of this image well-behaved as well.
trap 'exit 0' TERM INT

was_paused=""
while true; do
    if [ -f "${PAUSE_FILE}" ]; then
        # Log the transition, not every tick — otherwise a long pause buries
        # the log in identical lines.
        if [ "${was_paused}" != "yes" ]; then
            echo "$(date +%H:%M:%S)  PAUSED — autoswitch held off, manual choice stands"
            was_paused="yes"
        fi
    else
        if [ "${was_paused}" = "yes" ]; then
            echo "$(date +%H:%M:%S)  RESUMED — autoswitch active"
        fi
        was_paused="no"
        # Never let a failed tick kill the loop: a transient network or lock
        # error must not leave the engine permanently dead.
        cswap auto --once || true
    fi
    sleep "${TICK_S}"
done
