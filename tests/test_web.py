"""Tests for the web dashboard (`cswap web`).

Every case here pins a defect an adversarial review actually found in this
module, so each one should be read as "this regressed once already".
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from claude_swap import paths
from claude_swap.exceptions import ConfigError
from claude_swap.settings import set_setting
from claude_swap.update_check import _parse_version
from claude_swap.web import server as web_server


class TestParseVersion:
    """A fork carries a PEP 440 local version; that must not kill the notifier."""

    def test_plain_release(self):
        assert _parse_version("0.25.0") == (0, 25, 0)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # The exact string a self-built fork carries. This used to raise
            # ValueError, which check_for_update swallows -- so the builds most
            # likely to be behind were the ones permanently told nothing.
            ("0.26.0b1+web.1", (0, 26, 0)),
            ("0.26.0b1", (0, 26, 0)),
            ("1.2.3+local", (1, 2, 3)),
            ("1.2.3rc1", (1, 2, 3)),
        ],
    )
    def test_local_and_prerelease_versions_parse(self, raw, expected):
        assert _parse_version(raw) == expected

    def test_ordering_still_works_across_a_local_version(self):
        assert _parse_version("0.26.0b1+web.1") > _parse_version("0.25.0")

    def test_garbage_still_raises(self):
        with pytest.raises(ValueError):
            _parse_version("not-a-version")


class TestThresholdBounds:
    """The slider posts a number; the CLI's own spec is what must gate it."""

    def test_valid_value_persists(self, tmp_path):
        assert set_setting(tmp_path, "autoswitch.threshold", "85") == 85.0

    @pytest.mark.parametrize("bad", ["200", "49.9", "0", "-5", "abc", "nan", "inf"])
    def test_out_of_range_or_unparseable_is_refused(self, tmp_path, bad):
        with pytest.raises(ConfigError):
            set_setting(tmp_path, "autoswitch.threshold", bad)

    def test_upper_bound_of_the_spec_is_reachable(self, tmp_path):
        # The slider's max must be able to express this; it shipped as 99.
        assert set_setting(tmp_path, "autoswitch.threshold", "99.9") == 99.9


class TestPauseFileIsShared:
    """The pause toggle was a no-op for anyone not using the Docker loop."""

    def test_dashboard_and_engine_consult_the_same_file(self, tmp_path, monkeypatch):
        """The property the fix is about: what the dashboard WRITES is what the
        engine READS. A module-level constant froze Path.home() at import while
        the engine resolved it at tick time, so the two could diverge."""
        monkeypatch.setattr(
            paths.Path, "home", staticmethod(lambda: tmp_path)
        )
        web_server.set_autoswitch_paused(True)
        # Read back through the engine's own accessor, not the server's.
        assert paths.autoswitch_pause_file().exists()
        assert paths.autoswitch_pause_file().parent == tmp_path

        # And it must follow a later home change rather than a snapshot.
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: other))
        assert web_server.autoswitch_paused() is False

    def test_toggle_round_trips(self, tmp_path, monkeypatch):
        flag = tmp_path / ".cswap-web-paused"
        monkeypatch.setattr(paths, "autoswitch_pause_file", lambda: flag)
        assert web_server.autoswitch_paused() is False
        web_server.set_autoswitch_paused(True)
        assert flag.exists()
        assert web_server.autoswitch_paused() is True
        web_server.set_autoswitch_paused(False)
        assert not flag.exists()
        assert web_server.autoswitch_paused() is False


class _StubService:
    """Enough surface for the handler; touches no store."""

    def __init__(self):
        self.threshold_calls: list[str] = []

    def latest(self) -> dict:
        return {"accounts": [], "threshold": 90.0}

    def set_threshold(self, raw: str) -> dict:
        self.threshold_calls.append(raw)
        return {"ok": True, "message": "ok", "output": "", "payload": None}


@pytest.fixture
def live_server():
    """A real socket, because the bug under test is in header handling."""
    service = _StubService()
    handler = type(
        "BoundHandler",
        (web_server.Handler,),
        {"service": service, "token": "t", "port": 0, "no_auth": True},
    )
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], service
    httpd.shutdown()
    httpd.server_close()


def _post_raw(
    port: int,
    body: bytes,
    content_length: str | None,
    *,
    path: str = "/api/threshold",
    want_body: bool = False,
    extra: dict[str, str] | None = None,
) -> int | tuple[int, bytes]:
    """POST with hand-written framing, bypassing the client's own."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest(
        "POST", path, skip_accept_encoding=True, skip_host=True
    )
    conn.putheader("Host", "127.0.0.1")
    conn.putheader("Content-Type", "application/json")
    if content_length is not None:
        conn.putheader("Content-Length", content_length)
    for k, v in (extra or {}).items():
        conn.putheader(k, v)
    conn.endheaders()
    conn.send(body)
    try:
        resp = conn.getresponse()
        payload = resp.read()
        return (resp.status, payload.strip()) if want_body else resp.status
    finally:
        conn.close()


class TestContentLengthIsBoundedBothWays:
    """A negative Content-Length cleared an upper-bound-only check, stayed
    truthy, and reached rfile.read(-1) -- "read to EOF" -- so the 64 KB cap was
    skipped entirely and the handler thread could block forever."""

    def test_negative_content_length_is_refused(self, live_server):
        port, service = live_server
        body = json.dumps({"value": 85}).encode()
        status, reason = _post_raw(port, body, "-1", want_body=True)
        assert status == 400
        # Assert the REASON, not just the code: a wrong fix that quietly
        # discards the body also answers 400, and would pass a code-only check.
        assert reason == b"bad Content-Length"
        assert service.threshold_calls == []

    def test_non_integer_content_length_is_refused_not_500(self, live_server):
        port, service = live_server
        status, reason = _post_raw(port, b"{}", "abc", want_body=True)
        assert status == 400
        assert reason == b"bad Content-Length"
        assert service.threshold_calls == []

    def test_oversized_body_is_still_refused(self, live_server):
        port, _ = live_server
        assert _post_raw(port, b"{}", "64001") == 413

    def test_a_normal_body_still_works(self, live_server):
        port, service = live_server
        body = json.dumps({"value": 85}).encode()
        assert _post_raw(port, body, str(len(body))) == 200
        assert service.threshold_calls == ["85"]


class TestThresholdPayloadValidation:
    def _post(self, port: int, payload: dict):
        body = json.dumps(payload).encode()
        return _post_raw(port, body, str(len(body)))

    @pytest.mark.parametrize("value", [[1, 2], {"a": 1}, None, True, False])
    def test_non_numeric_types_are_refused_before_reaching_settings(
        self, live_server, value
    ):
        port, service = live_server
        assert self._post(port, {"value": value}) == 400
        assert service.threshold_calls == []

    def test_numeric_string_is_accepted(self, live_server):
        port, service = live_server
        assert self._post(port, {"value": "85"}) == 200
        assert service.threshold_calls == ["85"]


class TestSnapshotCarriesThreshold:
    def test_threshold_is_projected(self):
        snap = type(
            "S", (), {"accounts": [], "active_number": None, "taken_at": 0.0}
        )()
        assert web_server.snapshot_to_json(snap, 90.0)["threshold"] == 90.0

    def test_missing_threshold_is_none_not_an_error(self):
        snap = type(
            "S", (), {"accounts": [], "active_number": None, "taken_at": 0.0}
        )()
        assert web_server.snapshot_to_json(snap, None)["threshold"] is None


class TestFramingRejections:
    """http.server does not decode chunked, so an undeclared body was silently
    dropped -- and a mutation then ran on its defaults."""

    def test_chunked_is_refused(self, live_server):
        port, service = live_server
        status = _post_raw(
            port, b'{"value":85}', None, extra={"Transfer-Encoding": "chunked"}
        )
        assert status == 411
        assert service.threshold_calls == []

    def test_a_bodyless_post_does_not_act_on_defaults(self, live_server):
        """`/api/autoswitch` with no body used to mean paused=False (resume) and
        `/api/switch-strategy` with no body used to mean a real rotation."""
        port, _ = live_server
        for route in ("/api/autoswitch", "/api/switch-strategy"):
            assert _post_raw(port, b"", "0", path=route) == 400


class TestEnginePauseGate:
    """The substance of the pause fix lives in autoswitch.tick(), which had no
    test at all -- the toggle was a no-op outside the Docker shell loop."""

    def test_tick_returns_no_action_and_does_not_poll_when_paused(
        self, tmp_path, monkeypatch
    ):
        from claude_swap.autoswitch import AutoSwitchEngine, TickOutcome

        monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: tmp_path))
        engine = AutoSwitchEngine.__new__(AutoSwitchEngine)  # no store, no network
        emitted: list = []
        engine._emit = lambda event: emitted.append(event)

        polled: list[bool] = []

        def _inner():
            polled.append(True)
            return TickOutcome.NO_ACTION

        engine._tick_inner = _inner

        # Flag absent: the gate must not swallow a normal tick.
        assert engine.tick() is TickOutcome.NO_ACTION
        assert polled == [True]

        # Flag present: short-circuits BEFORE polling, and says so.
        paths.autoswitch_pause_file().touch()
        assert engine.tick() is TickOutcome.NO_ACTION
        assert polled == [True], "polled while paused"
        assert any(getattr(e, "reason", "") == "paused" for e in emitted)

    def test_tick_polls_again_once_the_flag_is_gone(self, tmp_path, monkeypatch):
        from claude_swap.autoswitch import AutoSwitchEngine, TickOutcome

        monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: tmp_path))
        engine = AutoSwitchEngine.__new__(AutoSwitchEngine)
        engine._emit = lambda event: None
        engine._tick_inner = lambda: TickOutcome.SWITCHED

        flag = paths.autoswitch_pause_file()
        flag.touch()
        assert engine.tick() is TickOutcome.NO_ACTION
        flag.unlink()
        assert engine.tick() is TickOutcome.SWITCHED


class TestPageStructure:
    """Cheap structural guards for two defects that were only visible in a
    browser, so nothing in the suite would have caught either."""

    def _page(self) -> str:
        from claude_swap.web.server import INDEX

        return INDEX.read_text(encoding="utf-8")

    def test_hidden_attribute_actually_hides(self):
        # Author `display:flex` out-ranks the UA [hidden] rule, so without an
        # explicit winning rule `el.hidden = true` is a silent no-op.
        assert "[hidden] { display: none !important; }" in self._page()

    def test_threshold_control_is_not_inside_the_hero(self):
        # The hero is hidden whenever there is no active account or no usage.
        # Nesting the only threshold control there made it vanish, and leave
        # the tab order, exactly when a usage outage made it most useful.
        page = self._page()
        hero_open = page.index('<section class="hero"')
        hero_close = page.index("</section>", hero_open)
        assert 'id="thr"' not in page[hero_open:hero_close]
        assert 'id="thr"' in page
