"""Unit tests for the shared usage-poll cadence policy."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_swap import poll_policy

NOW = 1_000_000.0
HALF = lambda: 0.5  # noqa: E731 — rng midpoint: jitter factor exactly 1.0


def _usage(pct: float, resets_at: str | None = None) -> dict:
    window: dict = {"pct": pct}
    if resets_at:
        window["resets_at"] = resets_at
    return {"five_hour": window, "seven_day": {"pct": 0.0}}


def _plan(**overrides):
    kwargs = dict(
        prev_interval_s=None,
        prev_usage=None,
        new_usage=_usage(10),
        is_active=False,
        threshold=90.0,
        models=(),
        recent_429=False,
        now=NOW,
        rng=HALF,
    )
    kwargs.update(overrides)
    return poll_policy.plan_after_fetch(**kwargs)


class TestIntervalAdaptation:
    def test_first_fetch_uses_defaults(self):
        _, active = _plan(is_active=True)
        _, candidate = _plan(is_active=False)
        assert active == poll_policy.MIN_INTERVAL_S
        assert candidate == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_unmoved_decays_toward_the_ceiling(self):
        _, interval = _plan(prev_interval_s=300.0, prev_usage=_usage(10))
        assert interval == 450.0
        _, capped = _plan(prev_interval_s=500.0, prev_usage=_usage(10))
        assert capped == poll_policy.CANDIDATE_MAX_INTERVAL_S
        _, active_capped = _plan(
            prev_interval_s=250.0, prev_usage=_usage(10), is_active=True
        )
        assert active_capped == poll_policy.ACTIVE_MAX_INTERVAL_S

    def test_movement_halves_floored_at_min(self):
        _, interval = _plan(
            prev_interval_s=600.0, prev_usage=_usage(10), new_usage=_usage(15)
        )
        assert interval == 300.0
        _, floored = _plan(
            prev_interval_s=200.0, prev_usage=_usage(10), new_usage=_usage(15)
        )
        assert floored == poll_policy.MIN_INTERVAL_S

    def test_sub_delta_wiggle_is_not_movement(self):
        _, interval = _plan(
            prev_interval_s=300.0,
            prev_usage=_usage(10),
            new_usage=_usage(10.5),  # below MOVEMENT_DELTA_PCT
        )
        assert interval == 450.0

    def test_unknown_pct_uses_the_default(self):
        _, interval = _plan(prev_interval_s=600.0, new_usage=None)
        assert interval == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S


class TestUrgentMode:
    def _urgent_kwargs(self, **overrides):
        kwargs = dict(
            prev_interval_s=poll_policy.MIN_INTERVAL_S,
            prev_usage=_usage(78),
            new_usage=_usage(82),  # moving, inside the 75..90 band
            is_active=True,
            threshold=90.0,
        )
        kwargs.update(overrides)
        return kwargs

    def test_active_moving_in_band_goes_urgent(self):
        _, interval = _plan(**self._urgent_kwargs())
        assert interval == poll_policy.URGENT_INTERVAL_S

    def test_candidate_never_goes_urgent(self):
        _, interval = _plan(**self._urgent_kwargs(is_active=False))
        assert interval == poll_policy.MIN_INTERVAL_S  # plain movement halving

    def test_no_movement_no_urgency(self):
        _, interval = _plan(**self._urgent_kwargs(new_usage=_usage(78)))
        assert interval > poll_policy.URGENT_INTERVAL_S

    def test_below_the_band_no_urgency(self):
        _, interval = _plan(
            **self._urgent_kwargs(prev_usage=_usage(40), new_usage=_usage(50))
        )
        assert interval == poll_policy.MIN_INTERVAL_S

    def test_recent_429_suppresses_urgency(self):
        _, interval = _plan(**self._urgent_kwargs(recent_429=True))
        assert interval == poll_policy.POST_429_MIN_INTERVAL_S

    def test_urgent_then_unmoved_snaps_back_to_the_floor(self):
        # Once movement stops, the next interval is the normal floor — never
        # a sub-floor decay chain (60 → 90 → 135 …) off the urgent base.
        _, interval = _plan(
            **self._urgent_kwargs(
                prev_interval_s=poll_policy.URGENT_INTERVAL_S,
                new_usage=_usage(78),  # unmoved
            )
        )
        assert interval == poll_policy.MIN_INTERVAL_S


class TestPost429Floor:
    def test_recent_429_floors_the_cadence(self):
        _, interval = _plan(recent_429=True, prev_usage=_usage(10))
        assert interval >= poll_policy.POST_429_MIN_INTERVAL_S

    def test_slower_learned_cadence_survives_the_floor(self):
        # A learned interval already above the floor is grown (×1.5), never
        # dropped back to the floor.
        _, interval = _plan(
            recent_429=True, prev_interval_s=590.0, prev_usage=_usage(10)
        )
        assert interval == pytest.approx(590.0 * poll_policy.POST_429_BACKOFF_MULT)
        assert interval > poll_policy.POST_429_MIN_INTERVAL_S


class TestPost429Aimd:
    """AIMD backoff on a contended token: while 429s recur, each successful
    poll multiplicatively increases the interval toward a wider 429 ceiling, so
    independent machines sharing one token each retreat and their combined poll
    rate converges under the endpoint budget (no cross-machine coordination)."""

    def test_recent_429_multiplicatively_increases_from_prev(self):
        # A prior 360s interval, still seeing 429s, is pushed up (×1.5), not
        # held flat at the floor.
        _, interval = _plan(
            recent_429=True,
            prev_interval_s=poll_policy.POST_429_MIN_INTERVAL_S,  # 360
            prev_usage=_usage(10),
        )
        assert interval > poll_policy.POST_429_MIN_INTERVAL_S
        assert interval == pytest.approx(
            poll_policy.POST_429_MIN_INTERVAL_S * poll_policy.POST_429_BACKOFF_MULT
        )

    def test_recent_429_ceiling_exceeds_normal_candidate_max(self):
        # The 429 ceiling is wider than the normal candidate ceiling so a
        # contended token can back off far enough for several machines to fit.
        assert (
            poll_policy.POST_429_MAX_INTERVAL_S
            > poll_policy.CANDIDATE_MAX_INTERVAL_S
        )
        _, interval = _plan(
            recent_429=True,
            prev_interval_s=poll_policy.POST_429_MAX_INTERVAL_S,  # already at ceiling
            prev_usage=_usage(10),
        )
        assert interval == poll_policy.POST_429_MAX_INTERVAL_S

    def test_no_429_uses_normal_ceiling(self):
        # Without recent 429s the wider ceiling never applies (normal cadence).
        _, interval = _plan(
            recent_429=False, prev_interval_s=590.0, prev_usage=_usage(10)
        )
        assert interval == poll_policy.CANDIDATE_MAX_INTERVAL_S

    def _converge_trajectory(self, recent_429: bool, rounds: int = 12):
        # Deterministic evolution of the interval under a sustained contended
        # token: each successful poll re-plans with the same recent_429 flag and
        # unmoved usage (movement decay would only shorten it — the worst case
        # for convergence is an unmoving account that just keeps 429ing). rng at
        # the midpoint so jitter is exactly 1.0 and the trajectory is exact.
        prev = None
        traj = []
        for _ in range(rounds):
            _, interval = _plan(
                recent_429=recent_429,
                prev_interval_s=prev,
                prev_usage=_usage(10),
                new_usage=_usage(10),
            )
            traj.append(interval)
            prev = interval
        return traj

    def test_sustained_429_grows_the_interval_to_the_wide_ceiling(self):
        # THE convergence property: while 429s recur, the interval must keep
        # growing (×MULT) until it reaches POST_429_MAX_INTERVAL_S. This is what
        # lets N machines sharing one token each back off far enough that their
        # combined rate drops under the budget — the deadlock cannot clear
        # without it.
        traj = self._converge_trajectory(recent_429=True)
        # strictly increasing until it saturates at the wide ceiling
        assert traj[-1] == poll_policy.POST_429_MAX_INTERVAL_S
        assert traj == sorted(traj)  # monotonic non-decreasing
        # actually reaches the ceiling within the simulated rounds
        assert max(traj) == poll_policy.POST_429_MAX_INTERVAL_S
        # each pre-ceiling step grew by the multiplier (AIMD multiplicative incr)
        for a, b in zip(traj, traj[1:]):
            if b < poll_policy.POST_429_MAX_INTERVAL_S:
                assert b == pytest.approx(a * poll_policy.POST_429_BACKOFF_MULT)

    def test_without_recency_the_interval_is_capped_at_the_narrow_ceiling(self):
        # The failure mode the recency bug caused: if recent_429 is False on the
        # post-block success (the pre-fix behavior after an honored hour-scale
        # Retry-After), the interval can never exceed CANDIDATE_MAX_INTERVAL_S.
        # N machines then jam at 600s each and their combined rate can sit above
        # the budget forever — a permanent deadlock the AIMD is meant to break.
        traj = self._converge_trajectory(recent_429=False)
        assert max(traj) == poll_policy.CANDIDATE_MAX_INTERVAL_S
        assert (
            poll_policy.CANDIDATE_MAX_INTERVAL_S
            < poll_policy.POST_429_MAX_INTERVAL_S
        )


class TestResetCapping:
    def _iso(self, ts: float) -> str:
        return (
            datetime.fromtimestamp(ts, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )

    def test_poll_never_scheduled_past_a_future_reset(self):
        reset_ts = NOW + 90.0
        next_poll, interval = _plan(new_usage=_usage(40, self._iso(reset_ts)))
        assert next_poll == pytest.approx(reset_ts + poll_policy.RESET_SLACK_S)
        assert interval == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_at_limit_keeps_bounded_polling_before_distant_reset(self):
        reset_ts = NOW + 7_200.0
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)
        assert next_poll < reset_ts

    def test_at_limit_poll_is_pulled_to_an_imminent_reset(self):
        reset_ts = NOW + 90.0
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(reset_ts + poll_policy.RESET_SLACK_S)

    @pytest.mark.parametrize("reset_ts", [NOW - 90.0, NOW])
    def test_at_limit_ignores_non_future_reset(self, reset_ts):
        next_poll, interval = _plan(new_usage=_usage(100, self._iso(reset_ts)))
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)

    def test_active_at_limit_uses_same_bounded_recovery_probe(self):
        reset_ts = NOW + 7_200.0
        next_poll, interval = _plan(
            new_usage=_usage(100, self._iso(reset_ts)), is_active=True
        )
        assert interval == poll_policy.EXHAUSTED_INTERVAL_S
        assert next_poll == pytest.approx(NOW + interval)


class TestJitter:
    def test_jitter_bounds(self, monkeypatch):
        monkeypatch.setattr("claude_swap.poll_policy.JITTER_FRAC", 0.1)
        early, _ = _plan(rng=lambda: 0.0)
        late, _ = _plan(rng=lambda: 1.0)
        interval = poll_policy.CANDIDATE_DEFAULT_INTERVAL_S
        assert early == pytest.approx(NOW + interval * 0.9)
        assert late == pytest.approx(NOW + interval * 1.1)


class TestBudgetInvariants:
    """Relationships the measured rate limit demands of the constants.

    Measured 2026-07-11 (probe3): a rolling ~60-minute window of ~28-30
    requests per token × UA-class — not a refilling bucket. Capacity returns
    only as old requests age out of the trailing hour, so a saturated window
    needs up to 60 minutes to recover. These invariants lean only on the
    robust parts of that measurement (a safe sustained cadence and an
    hour-scale recovery horizon), not on the exact server algorithm.
    """

    def test_sustained_floor_stays_under_the_hourly_cap(self):
        # 3600/180 = 20 requests/hour vs the measured ~28-30/hour window.
        assert poll_policy.MIN_INTERVAL_S >= 180.0
        assert poll_policy.SERVE_TTL_S >= 180.0

    def test_edge_backoff_probes_slower_than_capacity_frees(self):
        # While saturated, capacity returns at up to ~30/hour as the old
        # burst ages out; probing at ≥300 s (≤12/hour) lets recovery win.
        assert poll_policy.EDGE_BACKOFF_S >= 300.0

    def test_post_429_floor_covers_the_saturation_horizon(self):
        # A 429 means the trailing hour is full, and it takes up to 60
        # minutes for the spending burst to age out entirely.
        assert poll_policy.RECENT_429_WINDOW_S >= 3600.0
        assert poll_policy.POST_429_MIN_INTERVAL_S >= poll_policy.MIN_INTERVAL_S

    def test_urgent_episode_alone_fits_inside_the_window_cap(self):
        # Urgent mode is bounded by construction: each further urgent poll
        # needs ≥ MOVEMENT_DELTA_PCT of movement, so the slowest qualifying
        # burn crosses the escalation band in margin/delta polls — inside the
        # ~28-30 request rolling-hour window even before the post-429 floor
        # (which absorbs any overshoot) is considered.
        polls = poll_policy.ESCALATION_MARGIN_PCT / poll_policy.MOVEMENT_DELTA_PCT
        assert polls < 27


class TestOrgBudgetIsDividedAmongPeers:
    """N accounts in one organization draw on ONE request budget, so each
    one's cadence is the org's budget divided by them. Regression cover for
    the 18-hour outage of 2026-09-18: four accounts each planning their own
    ~20 requests/hour put ~33/hour on a ~28-30/hour cap and never recovered."""

    def test_a_lone_account_is_unscaled(self):
        for peers in (0, 1):  # 0 is a caller that could not count; never < 1x
            _, active = _plan(is_active=True, peers=peers)
            _, candidate = _plan(is_active=False, peers=peers)
            assert active == poll_policy.MIN_INTERVAL_S
            assert candidate == poll_policy.CANDIDATE_DEFAULT_INTERVAL_S

    def test_the_widening_is_per_role_not_per_account(self):
        # Was `== MIN_INTERVAL_S * 4` for the active row. That even split is
        # what let an active row cross the whole escalation band between two
        # polls on 2026-09-18, so the active row now keeps a fast lane and the
        # candidates absorb its share.
        _, active = _plan(is_active=True, peers=4)
        assert active == poll_policy.ACTIVE_FAST_LANE_S
        assert active < poll_policy.MIN_INTERVAL_S * 4
        _, candidate = _plan(is_active=False, peers=4)
        assert candidate > poll_policy.CANDIDATE_DEFAULT_INTERVAL_S * 4 - 1
        assert candidate == poll_policy.POST_429_MAX_INTERVAL_S  # clamped

    def test_the_summed_rate_lands_under_the_measured_cap(self):
        # The property the constants exist to hold: whatever each account
        # plans, the ORG's total must fit the ~28-30 requests/hour the
        # endpoint was measured to allow.
        peers = 4
        _, active = _plan(is_active=True, peers=peers)
        _, candidate = _plan(is_active=False, peers=peers)
        per_hour = 3600.0 / active + (peers - 1) * (3600.0 / candidate)
        assert per_hour <= 28.0
        # ...and the unscaled planner is what broke it, so the test is not
        # vacuously satisfied by any interval at all.
        _, un_active = _plan(is_active=True, peers=1)
        _, un_candidate = _plan(is_active=False, peers=1)
        assert 3600.0 / un_active + (peers - 1) * (3600.0 / un_candidate) > 28.0

    def test_urgent_mode_is_divided_too(self):
        # Urgent mode is bounded "by construction" only against a budget it
        # has room in; at peers>1 the steady traffic already fills the org's
        # share, so the burst has to scale with everything else.
        _, urgent = _plan(
            is_active=True,
            peers=4,
            prev_interval_s=poll_policy.MIN_INTERVAL_S * 4,
            prev_usage=_usage(80.0),
            new_usage=_usage(85.0),
            threshold=90.0,
        )
        # Still priced on the EVEN split, deliberately. Pricing it on the
        # active share instead gave 100s, which while armed is 44 requests/hour
        # for the org -- past the measured cap, i.e. the 429 episode again.
        assert urgent == poll_policy.URGENT_INTERVAL_S * 4
        # And the consequence of the active row sitting at SERVE_TTL_S: urgent
        # mode is now SLOWER than the normal plan, so it is no longer what
        # saves a fast burn -- the floor is. Left in place because it still
        # binds at peer counts where the fast lane has run out, and because a
        # plan is never widened past its own ceiling anyway.
        _, normal = _plan(is_active=True, peers=4)
        assert urgent > normal
        assert normal == poll_policy.SERVE_TTL_S
        burn_per_poll = poll_policy.REFERENCE_BURN_PCT_PER_MIN * (normal / 60.0)
        assert burn_per_poll < poll_policy.ESCALATION_MARGIN_PCT

    def test_no_scaled_interval_outlives_the_trust_it_is_read_under(self):
        # A planned interval wider than usage_store.TRUST_MAX_AGE_S would age
        # the row into "unknown" purely from cadence, which autoswitch reads as
        # failover pressure. The clamp is what forbids it at any peer count.
        from claude_swap import usage_store

        for peers in (1, 4, 12, 500):
            for is_active in (True, False):
                _, interval = _plan(is_active=is_active, peers=peers)
                assert interval <= poll_policy.POST_429_MAX_INTERVAL_S
                assert interval < usage_store.TRUST_MAX_AGE_S

    def test_active_ceiling_s_is_what_consumers_must_test_against(self):
        # autoswitch distinguishes "a leftover candidate plan" from a correct
        # active plan by width. At peers>1 a correct active plan is wider than
        # the bare constant, so the bare constant would misread every one.
        # prev_usage supplied so the decay branch runs: without it the
        # planner returns `default` and never consults the ceiling.
        _, active = _plan(
            is_active=True,
            peers=4,
            prev_interval_s=99_999.0,
            prev_usage=_usage(10),
            new_usage=_usage(10),
        )
        # At four accounts the fast lane pins the active share to 1.0, so the
        # ceiling now EQUALS the unscaled constant. What the consumer needs is
        # unchanged: the ceiling still separates a correct active plan from a
        # leftover candidate one, which is the distinction autoswitch draws.
        assert active <= poll_policy.active_ceiling_s(4)
        assert poll_policy.active_ceiling_s(4) == poll_policy.ACTIVE_MAX_INTERVAL_S
        assert poll_policy.candidate_interval_s(4) > poll_policy.active_ceiling_s(4)
        # Past the fast lane it widens again, which is why the helper exists.
        assert poll_policy.active_ceiling_s(12) > poll_policy.ACTIVE_MAX_INTERVAL_S


class TestConsumersDoNotDriftFromTheCadence:
    """The drift guard. Read this before changing any cadence constant.

    Every surface that judges a reading "stale" has to agree with the cadence
    poll_policy actually plans. Twice now one did not: the web banner kept a
    fixed 900s line and the TUI a fixed 300s one, both sized against cadences
    that predate `budget_share`. At four accounts in one org the policy plans
    720-1800s, so both surfaces called a perfectly healthy engine stale -- the
    banner then blamed a suspended laptop, which cost real debugging time on
    2026-09-18.

    These cases fail the moment a consumer is judged against anything but the
    plan the row itself carries, at any peer count -- which is the only way
    this stays fixed as the cadence keeps moving.
    """

    # Every cadence a plan can legitimately carry, at each peer count.
    @staticmethod
    def _plannable(peers):
        cand = poll_policy.candidate_share(peers)
        return {
            "active_min": poll_policy.active_min_interval_s(peers),
            "active_ceiling": poll_policy.active_ceiling_s(peers),
            "candidate_min": min(
                poll_policy.MIN_INTERVAL_S * cand,
                poll_policy.POST_429_MAX_INTERVAL_S,
            ),
            "candidate_ceiling": min(
                poll_policy.CANDIDATE_MAX_INTERVAL_S * cand,
                poll_policy.POST_429_MAX_INTERVAL_S,
            ),
            "post_429_ceiling": poll_policy.POST_429_MAX_INTERVAL_S,
        }

    @pytest.mark.parametrize("peers", [1, 2, 3, 4, 8, 16])
    def test_the_web_banner_accepts_every_plan_the_policy_can_make(self, peers):
        from claude_swap.web import server as web_server

        for name, interval in self._plannable(peers).items():
            # A row that polled exactly on schedule: as old as its own plan.
            row = {"ageSeconds": interval, "pollIntervalS": interval}
            assert not web_server._is_behind_plan(row), (
                f"peers={peers} {name}={interval}s: a row polling on schedule "
                f"was called stale -- the banner has drifted from the cadence"
            )

    @pytest.mark.parametrize("peers", [1, 2, 3, 4, 8, 16])
    def test_the_tui_does_not_dim_a_row_polling_on_schedule(self, peers):
        from claude_swap.tui.widgets import stale_measurement
        from claude_swap.usage_store import UsageEntry

        for name, interval in self._plannable(peers).items():
            # trust_extended is what the store sets while now < nextPollAt.
            usage = UsageEntry(age_s=interval, trust_extended=True)
            assert not stale_measurement(usage), (
                f"peers={peers} {name}={interval}s: a row polling on schedule "
                f"was dimmed -- the TUI has drifted from the cadence"
            )

    def test_a_row_genuinely_past_its_plan_is_still_flagged_everywhere(self):
        """The guard must not have been bought by never flagging anything."""
        from claude_swap.tui.widgets import stale_measurement
        from claude_swap.usage_store import UsageEntry
        from claude_swap.web import server as web_server

        assert web_server._is_behind_plan({"ageSeconds": 5000.0, "pollIntervalS": 300.0})
        assert stale_measurement(UsageEntry(age_s=5000.0, trust_extended=False))


class TestTheActiveRowCannotCrossTheBandUnobserved:
    """The 2026-09-18 miss, as a test.

    Account 1 was active, held 0.24 %/min for an hour, then accelerated to
    2.56 %/min and went 77% -> 100% of its 5h window. At the even split its
    cadence was 720s, so the next look was 12 minutes away: it passed 82% (the
    band edge), passed the 97% threshold and hit 100% with the engine still
    reading 77%. Urgent mode never armed, because arming needs a poll to LAND
    inside the band, and no poll did. The user was refused by Claude Code and
    switched by hand; `autoswitch_state.json` still named a switch from four
    days earlier.

    The invariant that was missing: the active row's cadence must be short
    enough that the band cannot be traversed between two polls.
    """

    MEASURED_BURN_PCT_PER_MIN = poll_policy.REFERENCE_BURN_PCT_PER_MIN
    THRESHOLD = 97.0
    # Where the request budget can still afford a cadence that covers the
    # reference burn. Past this the fast lane runs out; see
    # test_the_band_stops_covering_the_reference_burn_past_this_size.
    COVERED_PEERS = [1, 2, 3, 4, 5, 6, 7, 8]

    @pytest.mark.parametrize("peers", COVERED_PEERS)
    def test_the_band_is_wider_than_one_interval_of_the_measured_burn(self, peers):
        interval_min = poll_policy.active_interval_s(peers) / 60.0
        burned = self.MEASURED_BURN_PCT_PER_MIN * interval_min
        assert burned < poll_policy.ESCALATION_MARGIN_PCT, (
            f"peers={peers}: an active row burns {burned:.1f} points between "
            f"polls but the band is only {poll_policy.ESCALATION_MARGIN_PCT} "
            f"wide — it can cross the band unobserved and urgent mode will "
            f"never arm, which is the 2026-09-18 miss"
        )

    @pytest.mark.parametrize("peers", COVERED_PEERS)
    def test_band_coverage_is_stated_and_beats_the_measured_burn(self, peers):
        assert poll_policy.band_covers_pct_per_min(peers) > (
            self.MEASURED_BURN_PCT_PER_MIN
        )

    def test_the_band_stops_covering_the_reference_burn_past_this_size(self):
        """The limit, recorded rather than hidden.

        Beyond eight accounts on one budget the candidates are already against
        POST_429_MAX_INTERVAL_S, so the active row cannot keep its fast lane
        without overspending, and the band cannot be widened to compensate
        (escalation beats candidate plans -- see poll_policy). An org this
        large gets late switches on a fast burn; that is a property of the
        request budget, not a bug to fix here. If this assertion ever starts
        failing, the budget or the endpoint's shape changed -- re-derive.
        """
        assert poll_policy.band_covers_pct_per_min(9) < (
            self.MEASURED_BURN_PCT_PER_MIN
        )
        assert max(self.COVERED_PEERS) == 8

    def test_the_even_split_fails_this_invariant(self):
        """Not vacuous: the cadence this replaced does cross the band."""
        even_interval_min = (poll_policy.MIN_INTERVAL_S * 4) / 60.0  # the old 720s
        burned = self.MEASURED_BURN_PCT_PER_MIN * even_interval_min
        assert burned > poll_policy.ESCALATION_MARGIN_PCT

    def test_urgent_arms_on_the_poll_that_lands_in_the_band(self):
        """And once armed it is fast enough to catch the rest of the climb."""
        band_edge = self.THRESHOLD - poll_policy.ESCALATION_MARGIN_PCT
        _, urgent = _plan(
            is_active=True,
            peers=4,
            prev_interval_s=poll_policy.active_interval_s(4),
            prev_usage=_usage(band_edge),
            new_usage=_usage(band_edge + 2.0),
            threshold=self.THRESHOLD,
        )
        climb = self.MEASURED_BURN_PCT_PER_MIN * (urgent / 60.0)
        remaining = self.THRESHOLD - (band_edge + 2.0)
        assert climb < remaining, (
            f"urgent cadence {urgent}s burns {climb:.1f} points but only "
            f"{remaining:.1f} remain to the threshold"
        )


class TestTheBudgetStaysInsideWhatWasMeasured:
    """The rate is a deliberate, documented number — not "whatever comes out".

    It is NOT rate-neutral against the even split any more: the operator chose
    to spend reserve for a 3-minute active cadence (2026-09-18). What must hold
    is the measured evidence in poll_policy's own docstring: three accounts ran
    a month at 27/hour with zero 429s, and the episode that broke was 33/hour.
    So the planned total must stay below the figure proven clean, not merely
    below the cap.
    """

    PROVEN_CLEAN_PER_HOUR = 27.0
    OBSERVED_FAILURE_PER_HOUR = 33.0

    @pytest.mark.parametrize("peers", [1, 2, 3, 4, 5, 8, 12])
    def test_the_planned_total_stays_below_the_rate_proven_clean(self, peers):
        rate = 3600.0 / poll_policy.active_interval_s(peers) + (peers - 1) * (
            3600.0 / poll_policy.candidate_interval_s(peers)
        )
        assert rate < self.PROVEN_CLEAN_PER_HOUR, (
            f"peers={peers}: {rate:.1f} req/hour, and only {self.PROVEN_CLEAN_PER_HOUR} "
            f"has been observed to run clean for a month "
            f"({self.OBSERVED_FAILURE_PER_HOUR} is where 429s started)"
        )

    def test_the_active_row_gets_the_fastest_cadence_the_store_permits(self):
        """What the operator actually asked for, and why 2 minutes was refused:
        SERVE_TTL_S is the floor — anything fresher is served from the store
        without a request, so no setting can poll faster than this."""
        assert poll_policy.active_interval_s(4) == poll_policy.SERVE_TTL_S
        assert poll_policy.ACTIVE_FAST_LANE_S == poll_policy.SERVE_TTL_S

    @pytest.mark.parametrize("peers", [1, 2, 3, 4, 6, 8, 12])
    def test_the_summed_floor_rate_stays_under_the_measured_cap(self, peers):
        """Every row at its FLOOR — the worst case, all of them moving."""
        _, active = _plan(is_active=True, peers=peers, prev_usage=_usage(10),
                          new_usage=_usage(30))
        _, candidate = _plan(is_active=False, peers=peers, prev_usage=_usage(10),
                             new_usage=_usage(30))
        per_hour = 3600.0 / active + (peers - 1) * (3600.0 / candidate)
        assert per_hour <= 28.0, f"peers={peers}: {per_hour:.1f} req/hour"
