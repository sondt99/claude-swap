# deploy — always-on cswap

The browser dashboard (`cswap web`) plus an always-on autoswitch engine, as two
containers.

The dashboard itself is no longer here: it was vendored into the package as
`src/claude_swap/web/` and ships inside the wheel. This directory holds only the
deployment wrapper.

**Neither container ships claude-swap.** Both run the uv tool venv out of your
mounted home directory, so they execute the *same* install as your host `cswap`
— one copy, nothing to pin, nothing to bump in lockstep, and no chance of a
containerised copy migrating the shared on-disk store to a schema the host tool
doesn't understand. The image contributes only two shell drivers and an
entrypoint that puts that venv on `PATH`.

Build and install the wheel first — that is what the containers execute:

```
uv build && uv tool install --force ./dist/claude_swap-*.whl
```

Then, from this directory:

```
cp .env.example .env && chmod 600 .env   # first run only; fill in the values
docker compose up --build -d      # start
docker compose logs -f auto       # watch the autoswitch engine
docker compose down               # stop
```

    http://127.0.0.1:8787/

No token, no login — `CSWAP_WEB_NO_AUTH: "1"` in `docker-compose.yml`. See
[Security](#security) for exactly what that does and does not give up, and how
to turn auth back on.

## What it does

Read path and mutations both go through the hooks claude-swap documents for
alternate front-ends, so no switching, OAuth, or usage logic is reimplemented:

| Hook | Used for |
|---|---|
| `SnapshotSource.take()` | the account grid ("the supported read path for dashboards and GUI shells") |
| `run_action(partial(switcher.X))` | switch / disable / alias, captured |
| `json_output.usage_to_json()` | the usage projection, identical to `cswap list --json` |

One poller thread takes a snapshot every 10s and fans it out over SSE, so N open
tabs still cost one collect pass. The usage store gates real network calls per
account, so most ticks are free.

In the UI: switch to a slot, rotate, next-available, switch-to-best,
enable/disable, set alias. **Not** in the UI: `cswap add` (needs the interactive
OAuth login) and `cswap run` (sets env for one terminal — a web page cannot set
your shell's environment). Use the CLI for those.

## Running cswap inside the container

`docker exec` bypasses `ENTRYPOINT`, so the venv is not on `PATH` and plain
`docker exec cswap-web cswap list` fails with `not found`. Go through the
entrypoint:

    docker exec cswap-web /usr/local/bin/docker-entrypoint.sh cswap list

Or just run `cswap` on the host — same install, same store.

## Autoswitch

Configured on the host, in the shared `settings.json`. No restart needed — the
tick loop runs a fresh `cswap auto --once` each time, so settings are re-read
every tick:

    cswap config set autoswitch.threshold 90
    cswap config set autoswitch.intervalSeconds 15
    cswap config set autoswitch.cooldownSeconds 60
    cswap config set autoswitch.hysteresisPct 5

(`cswap auto`'s own loop reads settings *once at startup*, which is one of the
two reasons `autoswitch-loop.sh` replaces it — see that file.)

### Pause

The **Auto: ON / OFF** button in the dashboard holds the engine off so a
hand-picked account stays active. It works by touching `~/.cswap-web-paused`;
the tick loop skips while that file exists. A file rather than a flag because
the engine runs in a different container — both see it through the shared home
mount. Equivalent from a shell:

    touch ~/.cswap-web-paused     # pause
    rm ~/.cswap-web-paused        # resume

While paused you have **no rate-limit protection**, so the dashboard shows a
standing amber banner rather than a quiet toggle state.

### Why one threshold governs two very different windows

`threshold` binds on `max(5h, 7d)`, and those two move at completely different
speeds — the 5h window at ~1.6 %/min under load, the weekly one over days. A
threshold low enough to protect the fast window will strand an account whose
*weekly* number is high even when its 5h window is completely fresh. Seen here:
account 3 sat at `5h 0% / 7d 80%` and was ejected within seconds of every
manual selection, because binding 80 ≥ threshold 80.

There is no per-window threshold upstream. The practical resolution is to keep
the threshold above the highest weekly figure you still want to use, and rely
on urgent-mode polling (60s) for the 5h margin — at 1.6 %/min that costs about
1.6 points of overshoot, so 90 switches at roughly 91.6% worst case.

Three things to know about the threshold:

1. **99.9 is the hard maximum** — `cswap config set autoswitch.threshold 99.99`
   is rejected with "must be between 50 and 99.9".
2. **It binds on `max(5h, 7d)`**, not on 5h alone. An account at 7d 90% is
   already closer to tripping than its 5h number suggests.
3. **99.9 is deliberately later than upstream's default of 90.** The upstream
   docstring says 90 was picked over 95 to leave "margin for ... heavy subagent
   turns burning past the mark before a swap lands". At 99.9 you switch at the
   wall, so a burst can exhaust the window before the swap lands.
   `intervalSeconds 15` (the minimum) narrows that gap but cannot close it.

Also note `autoswitch.hysteresisPct`: a candidate must beat the active account's
headroom by that margin. At threshold 90 / hysteresis 5, a candidate needs to be
below ~85% to be picked.

### The threshold is not a cap

A threshold of 90 does **not** strand the last 10% of each account. It is a
routing preference that only applies while some account is still healthy. Once
*every* account is at or above the threshold, `autoswitch.py` flips the goal
from "most headroom" to "soonest back":

> When NOTHING is below the threshold — the active account and every candidate
> all in the 90s — "land somewhere healthy" has no answer, and holding out for
> one costs the user the session. Sitting still means burning the active account
> to 100% and taking a hard limit, with the peer that resets in 8 minutes never
> tried. So in that state the goal changes from "most headroom" to "soonest
> back": move to whichever account recovers first and keep working through its
> reset.

So the reserved margin is spent, not wasted — it is just spent last, and spent
on whichever account's 5h window returns first. Anti-flap in that mode is
`RECOVERY_HYSTERESIS_S = 300` (not the percentage margin), and
`SPENT_HEADROOM_PCT = 3.0` is the point below which a headroom edge is treated
as noise rather than a reason to move.

## Why the whole home directory is mounted

`docker-compose.yml` mounts `$HOME` at its identical path inside the container.
That is broader than it looks like it needs to be, and it is not laziness.

claude-swap publishes `~/.claude.json` by atomic rename (`switcher._write_json`
→ `fsutil.replace_with_retry` → `os.replace`). Renaming onto a Docker
**single-file** bind mount fails with `EBUSY`, and that helper only retries
Windows error codes — so on Linux it raises immediately and the switch dies.
Measured:

| Mount style | `os.replace` | Host sees the change |
|---|---|---|
| parent **directory** mounted | OK | yes |
| **single file** mounted | `EBUSY: Device or resource busy` | no |

And `~/.claude.json` lives at the *home root*, not inside `~/.claude/` (an
asymmetry `paths.py` calls out explicitly). So the directory that has to be
mounted for a switch to land is `$HOME` itself. Symlinks and hardlinks do not
help: `os.replace` swaps in a new inode, leaving any hardlink pointing at the
old one.

Mounting `$HOME` also happens to be what makes the host-venv trick work — the
uv tool venv and its CPython both live under `~/.local/share/uv/`, so they come
along for free with no extra mounts.

**The tradeoff is real**: these containers get read-write access to your entire
home directory, and containerising buys no isolation here, because the app's
whole job is mutating host credential files. If that bothers you, plain
`cswap web` does the same work natively, with no mount surface at all.

### Mount destination gotcha

This Docker only auto-creates **one level** of a missing mount destination.
`$HOME` → `/home/sondt23` works because `/home` already exists in the image. A
deeper destination (e.g. `/opt/a/b/c/d`) mounts **silently empty** — no error,
just nothing there. `mkdir -p` it in the Dockerfile if you ever need one.

## Troubleshooting: "the engine looks dead"

If `docker logs cswap-auto` shows nothing recent, check whether it is really
idle before concluding anything — Python block-buffers stdout whenever it is a
pipe, which under Docker it always is. Seen here: the last visible log line was
**three days old** while `autoswitch_state.json` recorded a switch minutes
earlier. The engine was fine; only its output was stuck in a buffer.

`PYTHONUNBUFFERED: "1"` in `docker-compose.yml` fixes it. To check liveness
independently of logs:

    docker top cswap-auto                                      # is the process there?
    cat ~/.local/share/claude-swap/autoswitch_state.json        # lastSwitchAt / lastSwitchTo

## Troubleshooting: "it switched too late"

Symptom: an account hits its 5h limit and the engine only reacts minutes later.
The engine ticks every `intervalSeconds`, but it decides on whatever the usage
store last managed to *fetch* — so the real question is data freshness, not tick
rate. Check it:

    python3 -c "
    import json,time; d=json.load(open('$HOME/.local/share/claude-swap/cache/usage.json')); n=time.time()
    [print(f\"#{k} fail={v.get('consecutiveFailures')} age={n-v.get('fetchedAt',n):.0f}s\") for k,v in sorted(d['accounts'].items())]"

Healthy looks like `fail=0` and `age` cycling under ~300s. If `fail` is nonzero
and `age` keeps climbing past ten minutes, fetches are failing and the store is
serving stale data behind a backoff.

**The cause seen here was a missing CA bundle in the image.** `debian:12-slim`
ships without `ca-certificates`, so every HTTPS call to `api.anthropic.com`
died with `CERTIFICATE_VERIFY_FAILED` — which cswap logs as a generic
`network` error, giving no hint that TLS trust is the problem. Fetch failures
went from ~2/hour to ~31/hour and usage aged to ~20 minutes. The Dockerfile now
installs `ca-certificates`; don't remove it. To confirm TLS from inside a
container:

    docker exec cswap-auto /usr/local/bin/docker-entrypoint.sh python -c \
      "import urllib.request;urllib.request.urlopen('https://api.anthropic.com/v1/models',timeout=10)"

`HTTP 401` is the healthy answer — it means TLS completed and the API answered.
`CERTIFICATE_VERIFY_FAILED` means the CA bundle is missing.

### How fresh can it get?

Not much fresher than ~3 minutes, and that is the API's constraint, not a
setting. `poll_policy.py` documents the usage endpoint's budget as **~28-30
requests per hour per identity**, and targets an average of **~1 request per 3
minutes**. The relevant floors: `MIN_INTERVAL_S = 180`,
`ACTIVE_MAX_INTERVAL_S = 300`, `URGENT_INTERVAL_S = 60` (bounded urgent mode,
entered within `ESCALATION_MARGIN_PCT = 15` points of the threshold). Polling
every 15s would be ~8× over the cap and would earn a 429 that blinds every
account for a full hour. This is exactly why `autoswitch.threshold` should sit
below 100: the margin covers the polling blind spot.

## Files

| File | Role |
|---|---|
| `Dockerfile` | ca-certificates + the two drivers; no Python, no app code |
| `docker-entrypoint.sh` | puts the host venv on `PATH`, fails fast if the mount is wrong |
| `autoswitch-loop.sh` | `cswap auto --once` on a timer, with a pause gate |
| `docker-compose.yml` | `web` (dashboard) + `auto` (autoswitch engine) |
| `.env.example` | template; copy to `.env` and fill in |
| `.env` | uid/gid/home/TZ + the dashboard token — **mode 600, gitignored** |

The dashboard itself lives in the package, not here:

| File | Role |
|---|---|
| `src/claude_swap/web/server.py` | HTTP + SSE layer; no switching logic of its own |
| `src/claude_swap/web/index.html` | single-file frontend, no external assets |

## Security

Auth is **off** by design here (`CSWAP_WEB_NO_AUTH: "1"`). These still hold, and
they are the ones that matter against the web:

| Control | Verified |
|---|---|
| published to `127.0.0.1:8787` only | `ss -ltnp` → `127.0.0.1:8787` |
| `Origin` rejected on mutations unless same-origin | cross-origin POST → **403** |
| `Referer` checked the same way | `Referer: https://evil.com` → **403** |
| `Host` pinned to loopback (DNS rebinding) | `Host: evil.com` → **403** |
| no CORS headers ever emitted | 0 `Access-Control-*` headers |
| CORS preflight unanswered | `OPTIONS` → **501**, so browsers block JSON POSTs |
| containers run as uid 1000 | store keeps host ownership and 0600 perms |

So a web page you visit **cannot** drive this: a cross-origin simple request
carries `Origin` and is rejected, and anything with a JSON content type is
preflighted, which this server never answers.

**What auth-off concedes is local processes.** With no token, anything running
on this machine can do:

    curl -X POST 127.0.0.1:8787/api/switch -d '{"identifier":"1"}'

No `Origin`, no cookie, no credentials needed — and it will switch the account.
On a machine that runs untrusted binaries (malware samples, CTF challenges,
target apps under analysis) that is a real surface, not a theoretical one.

### Turning auth back on

In `docker-compose.yml`, set `CSWAP_WEB_NO_AUTH: "0"`, then
`docker compose up -d`. Get the token and open it once:

    grep CSWAP_WEB_TOKEN deploy/.env
    # http://127.0.0.1:8787/?token=<token>

That first load pins the token into a `SameSite=Strict`, `HttpOnly` cookie
lasting **one year**, so the bookmark stays the bare `127.0.0.1:8787` from then
on. Hitting the bare URL without a valid cookie serves a page telling you where
the token lives (it never echoes the token itself).
