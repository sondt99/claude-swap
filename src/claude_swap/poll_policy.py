"""Cadence policy for the ``/api/oauth/usage`` endpoint — every number in one place.

The endpoint enforces a budget on non-first-party clients: a **~60-minute
window of ~28-30 requests per identity × UA-class** (measured 2026-07-11,
probe3, two runs: a rested identity admitted 30 requests before the first
429; the post-drain 429 oscillation ended exactly when the drain burst aged
60 minutes; steady 1/180 s polling then ran 96 minutes from a rested window
with zero 429s). It is NOT a bucket with a refill rate: capacity returns
only as old requests age out of the trailing hour, so a burst saturates the
identity for up to a full hour — pausing does not restore headroom early,
and earlier "refill rate" estimates were artifacts of measuring while
saturated.

What that identity is depends on which 429 regime the org is on — the two
regimes coexist across orgs (see the Retry-After discussion in
``usage_store``). Under the fixed-deadline regime it is the **account/org**
(measured 2026-07-28: a freshly minted token was blocked 135 s after issue,
which a per-token counter cannot produce). Under the saturated-edge
(``Retry-After: 0``) regime it is the **access token** (measured 2026-07-29,
probe4: at saturation, a freshly minted token of the same lineage was
admitted while the old token stayed blocked, requests interleaved). Plan for
the account-scoped case — it is the conservative one: re-authenticating
cannot be relied on to clear a block, and two machines holding different
tokens for one account may share one budget, which is what
``POST_429_BACKOFF_MULT`` below exists to converge.

ONE BUDGET, N ACCOUNTS. Every constant below sizes ONE account's cadence, but
the budget they are sized against belongs to the *identity*, and under the
fixed-deadline regime that identity is the account/org — so N accounts in one
organization draw on ONE budget, not N. Their rates ADD. Measured here
2026-09-18 on a 4-account org, from the store's own persisted intervals
(600/450/270/600s):

    3600/600 + 3600/450 + 3600/270 + 3600/600  =  33 requests/hour

against the ~28-30/hour cap — permanently over. The same store with 3 accounts
had run a month with zero 429s (27/hour, just under), and the first http-403
in that month's log landed ~18h after the 4th account was added. Saturation
then became self-sustaining: three accounts failing, each re-probing on the
store's generic 600s failure cap, is 18 requests/hour of pure waste that alone
holds the org at the cap. It ran 18 hours and did not recover on its own.

So the per-account cadence must be the org's budget DIVIDED by the accounts
sharing it: ``budget_share`` scales every floor and ceiling below by the peer
count, which keeps the module's ~20 requests/hour target a property of the ORG
rather than of each account. At peers=1 the scale is 1.0 and nothing moves.

This is scope-conservative by design. Whether the shared counter is really the
org or the egress IP (Cloudflare fronts the endpoint, and every collector on
one machine shares an IP) was NOT separable from the evidence above — both
predict exactly what was measured, and both are fixed by dividing the rate.
What would tell them apart is two machines on different networks holding the
same org's accounts; until someone measures that, treat the divisor as sound
and the *reason* for it as two candidates, not one.

Error bars: the horizon is bracketed to ~55-64 minutes from a single
transition event, the exact edge algorithm (likely a Cloudflare
sliding-window approximation) is undocumented, and Anthropic can retune it
any day — so the constants below lean only on the robust parts: a sustained
rate safely under the cap, and an ~hour recovery horizon. The budget target
is an **average of at most ~1 request / 3 minutes** (20/hour vs the ~28-30/
hour cap), leaving ~8-10 requests/hour of headroom for manual commands,
wake-from-sleep catch-up, and the bounded urgent mode below.

Health invariant to watch in the logs: steady state shows zero http-429.
A post-burst 429 does NOT reliably clear at its stated horizon — measured
over one machine's full log (re-measured 2026-08-03; re-derive rather than
trust this verbatim, it ages as the log grows — method recorded next to
``usage_store.RETRY_AFTER_MARGIN_S``), 20 of 35 lapsed blocks re-blocked
within 900s of their own deadline (+2s..+887s), each for a fresh full hour
("of 35", not 38 raw gaps: 3 are negative — NOT a uniform mechanism (one has
no within-block revision at all, one is revised BACKWARD mid-block, one is
unchanged — see usage_store.RETRY_AFTER_MARGIN_S's comment for the per-gap
detail) — excluded from both numerator and denominator; round 8 switched
from the prior "21 of 36"/"2 of 38" figures here to these, on the OTHER of
two equally-reproducing readings — see usage_store.RETRY_AFTER_MARGIN_S's
comment for which reading and why).
The prior "10 of 23" figure here did not reproduce under any of 40 method
variants swept and is superseded. That is why the wait is Retry-After plus
``usage_store.RETRY_AFTER_MARGIN_S`` and not Retry-After alone. What would
mean this model needs revisiting is a 429 episode at modest rates that
outlasts an hour *past* that margin.

Plans computed here are persisted per account in the usage store
(``nextPollAt``/``pollIntervalS``) by whichever collector fetched, so every
surface — ``cswap list``, the TUI, the menu bar, the auto engine — inherits
the same cadence no matter how often it repaints.

If a future probe revises the measured shape, adjust the constants in this
module only.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from datetime import datetime

from claude_swap import oauth

# Freshness floor shared by every collector: an entry younger than this is
# served from the store without any fetch, so the maximum sustained rate on
# one token is 1/SERVE_TTL_S regardless of how many surfaces are open.
SERVE_TTL_S = 180.0

# Normal cadence floor — movement can halve an interval down to this, never
# below.
MIN_INTERVAL_S = 180.0

# Urgent mode: the ACTIVE account, within ESCALATION_MARGIN_PCT of the
# switch threshold, with movement observed this poll (i.e. actually burning
# toward the limit). Bounded by construction: either the threshold is crossed
# (the engine switches away) or the movement stops (the next poll decays back
# to MIN_INTERVAL_S) — worst case margin/movement-delta ≈ 15 polls per
# episode, inside the measured ~28-30 request rolling-hour window; overshoot
# on top of steady traffic is absorbed by the post-429 floor below.
URGENT_INTERVAL_S = 60.0

# Decay ceilings for an account whose usage is not moving: the active account
# stays reasonably fresh, an idle alternate drifts out to ten minutes.
ACTIVE_MAX_INTERVAL_S = 300.0
CANDIDATE_DEFAULT_INTERVAL_S = 300.0
CANDIDATE_MAX_INTERVAL_S = 600.0

# Every interval above is one account's share of a budget that belongs to the
# org (see "ONE BUDGET, N ACCOUNTS" in the module docstring), so each is
# widened by the number of accounts drawing on it. Clamped at
# POST_429_MAX_INTERVAL_S — the widest cadence this module ever intends — so a
# large org can never plan a poll so far out that the row ages into "unknown"
# (usage_store.TRUST_MAX_AGE_S, 3600s) purely from cadence, which would hand
# the engine an unknown-usage account to fail over from.
def budget_share(peers: int) -> float:
    """Multiplier on every cadence floor/ceiling for ``peers`` accounts
    sharing one request budget. ``1.0`` for a lone account."""
    return float(max(1, peers))


def _scaled(interval_s: float, share: float) -> float:
    return min(interval_s * share, POST_429_MAX_INTERVAL_S)


def scaled_min_interval_s(peers: int) -> float:
    """The cadence floor at this peer count — what any caller writing a plan
    outside ``plan_after_fetch`` must use instead of ``MIN_INTERVAL_S``."""
    return _scaled(MIN_INTERVAL_S, budget_share(peers))


def active_ceiling_s(peers: int) -> float:
    """The widest cadence an ACTIVE account's plan can carry at this peer
    count. Consumers that test a persisted plan for "too slow to be an active
    plan" must use this, not ``ACTIVE_MAX_INTERVAL_S`` — at peers>1 a correct
    active plan is legitimately wider than the unscaled constant."""
    return _scaled(ACTIVE_MAX_INTERVAL_S, budget_share(peers))


# Exhaustion is stable enough to poll slowly, but not to stop polling until a
# reported reset. Quota grants and provider-side corrections can make an
# account usable before that timestamp, and decision-grade status must not age
# into "unavailable" while the scheduler is deliberately waiting. Ten-minute
# polling (six requests/hour) stays below the measured budget and
# detects recovery promptly; a nearer reported reset still pulls the next poll
# forward.
EXHAUSTED_INTERVAL_S = 600.0

# A window whose binding pct moved at least this much between polls is being
# consumed somewhere (this machine, another PC, session mode) → tighten; an
# unmoved one backs off toward its ceiling.
MOVEMENT_DELTA_PCT = 1.0

# ±fraction applied to each scheduled interval so independent processes
# (watch + menu bar + auto) drift apart instead of fetching in lockstep.
JITTER_FRAC = 0.1

# Reaction to a 429 with ``Retry-After: 0`` (the saturated-window edge):
# probe at most every 5 minutes (≤12/hour) so aging-out — up to ~30/hour —
# outpaces the probing (used by the usage store's failure backoff)...
EDGE_BACKOFF_S = 300.0
# ...and while any 429 was seen on the token within this window, floor the
# planned cadence here so freed capacity accumulates instead of being
# re-spent. The window matches the saturation horizon: a full trailing hour
# takes up to 60 minutes to age out.
POST_429_MIN_INTERVAL_S = 360.0
RECENT_429_WINDOW_S = 3600.0

# AIMD on a contended budget. The budget is shared across every machine
# polling the same account (under the account-scoped regime a machine with its
# own token is no less a competitor — see the module docstring on scope),
# none of them can see the others, and the endpoint
# exposes no remaining-request count — only a Retry-After once already
# blocked. So while 429s recur, each successful poll multiplicatively grows the
# interval (×POST_429_BACKOFF_MULT) toward POST_429_MAX_INTERVAL_S — wider than
# the normal candidate ceiling so several machines can each back off far enough
# that their combined rate fits under the budget. Movement (a real success run
# with no recent 429) decays it back down. This is TCP-style congestion control:
# the budget gets fair-shared by reaction alone, with no machine count or
# shared state to configure.
POST_429_BACKOFF_MULT = 1.5
POST_429_MAX_INTERVAL_S = 1800.0

# The engine escalates to a full candidate refresh when the active account is
# within this margin of the threshold (decision policy, but the urgent-mode
# cadence keys on the same band, so it lives with the cadence numbers).
ESCALATION_MARGIN_PCT = 15.0

# Never schedule a poll later than a known window reset (+ slack): stored
# usage is obsolete the moment the window rolls over.
RESET_SLACK_S = 60.0


def binding_pct(usage: dict | None, models: tuple[str, ...] = ()) -> float | None:
    """Utilization of the binding (worst) relevant window, or None."""
    headroom = oauth.account_headroom(usage, models)
    return None if headroom is None else 100.0 - headroom


def limiting_reset_ts(
    usage: dict | None, models: tuple[str, ...] = ()
) -> float | None:
    """Epoch when the last of the ≥100% relevant windows resets (account
    usable again)."""
    latest: float | None = None
    for _, pct, resets_at in oauth.relevant_windows(usage, models):
        if pct < 100.0:
            continue
        ts = parse_reset_ts(resets_at)
        if ts is not None and (latest is None or ts > latest):
            latest = ts
    return latest


def earliest_future_reset_ts(
    usage: dict | None, now: float, models: tuple[str, ...] = ()
) -> float | None:
    """Epoch of the next relevant-window reset ahead of ``now``, any
    utilization."""
    earliest: float | None = None
    for _, _, resets_at in oauth.relevant_windows(usage, models):
        ts = parse_reset_ts(resets_at)
        if ts is not None and ts > now and (earliest is None or ts < earliest):
            earliest = ts
    return earliest


def parse_reset_ts(resets_at: str | None) -> float | None:
    if not resets_at:
        return None
    try:
        return datetime.fromisoformat(
            str(resets_at).replace("Z", "+00:00")
        ).timestamp()
    except ValueError:
        return None


def plan_after_fetch(
    *,
    prev_interval_s: float | None,
    prev_usage: dict | None,
    new_usage: dict | None,
    is_active: bool,
    threshold: float,
    models: tuple[str, ...],
    recent_429: bool,
    now: float,
    peers: int = 1,
    rng: Callable[[], float] = random.random,
) -> tuple[float, float]:
    """``(next_poll_at, interval_s)`` for an account just fetched successfully.

    Movement (binding pct changed ≥ ``MOVEMENT_DELTA_PCT`` since the previous
    poll) halves the interval, floored at ``MIN_INTERVAL_S`` — or drops to
    ``URGENT_INTERVAL_S`` when the active account is moving inside the
    escalation band. No movement backs off ×1.5 toward the account's ceiling;
    unknown utilization uses the default. A recent 429 on this token floors
    the cadence at ``POST_429_MIN_INTERVAL_S`` (and suppresses urgent mode)
    until ``RECENT_429_WINDOW_S`` has passed. The scheduled time gets
    ``JITTER_FRAC`` noise, is never later than the account's next window
    reset (+ ``RESET_SLACK_S``). An at-limit account keeps a bounded slow
    poll instead of sleeping until that reset, so an early provider-side
    quota grant is observed and its decision-grade status stays current.

    ``peers`` is how many accounts share this one's request budget (see "ONE
    BUDGET, N ACCOUNTS" in the module docstring); every floor and ceiling
    below is widened by it, so the ~20 requests/hour target is the ORG's, not
    each account's. ``peers=1`` reproduces the unscaled behaviour exactly.
    """
    share = budget_share(peers)
    min_interval = _scaled(MIN_INTERVAL_S, share)
    urgent_interval = _scaled(URGENT_INTERVAL_S, share)
    default = _scaled(
        MIN_INTERVAL_S if is_active else CANDIDATE_DEFAULT_INTERVAL_S, share
    )
    ceiling = _scaled(
        ACTIVE_MAX_INTERVAL_S if is_active else CANDIDATE_MAX_INTERVAL_S, share
    )
    base = prev_interval_s or default
    prev_pct = binding_pct(prev_usage, models)
    new_pct = binding_pct(new_usage, models)
    if prev_pct is None or new_pct is None:
        moving = False
        interval = default
    elif abs(new_pct - prev_pct) >= MOVEMENT_DELTA_PCT:
        moving = True
        interval = max(min_interval, base / 2)
    else:
        # Floored so a sub-floor base (urgent mode's 60s) snaps straight back
        # to the normal cadence once movement stops, instead of decaying
        # through 90s/135s polls that the budget never intended.
        moving = False
        interval = min(ceiling, max(min_interval, base * 1.5))
    if (
        is_active
        and moving
        and not recent_429
        and new_pct is not None
        and new_pct >= threshold - ESCALATION_MARGIN_PCT
    ):
        interval = urgent_interval
    if recent_429:
        # AIMD additive-increase: grow the interval multiplicatively from the
        # last one toward the wider 429 ceiling, so machines sharing a
        # contended token each retreat until their combined rate fits the
        # budget. Floored at POST_429_MIN_INTERVAL_S for the first 429.
        increased = max(
            base * POST_429_BACKOFF_MULT, _scaled(POST_429_MIN_INTERVAL_S, share)
        )
        interval = min(POST_429_MAX_INTERVAL_S, max(interval, increased))

    headroom = oauth.account_headroom(new_usage, models)
    if headroom is not None and headroom <= 0:
        # Keep probing exhausted accounts: Anthropic can grant/reset quota
        # before the previously advertised timestamp. Preserve a wider
        # post-429 interval if congestion control already selected one.
        interval = max(interval, _scaled(EXHAUSTED_INTERVAL_S, share))

    next_poll = now + interval * (1.0 + JITTER_FRAC * (2.0 * rng() - 1.0))
    if headroom is not None and headroom <= 0:
        reset_ts = limiting_reset_ts(new_usage, models)
        if reset_ts is not None and reset_ts > now:
            next_poll = min(next_poll, reset_ts + RESET_SLACK_S)
    else:
        reset_ts = earliest_future_reset_ts(new_usage, now, models)
        if reset_ts is not None:
            next_poll = min(next_poll, reset_ts + RESET_SLACK_S)
    return next_poll, interval
