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

    def test_the_path_is_resolved_per_call_not_frozen_at_import(self, monkeypatch):
        """A module constant would snapshot Path.home() at import, so the
        dashboard could write one path while the engine — which resolves at
        tick time — consults another."""
        assert not hasattr(web_server, "PAUSE_FILE"), (
            "a cached constant reintroduces the dashboard/engine split"
        )
        monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: paths.Path("/a")))
        assert web_server.autoswitch_paused() is False
        monkeypatch.setattr(paths.Path, "home", staticmethod(lambda: paths.Path("/b")))
        # Resolving again must follow the new home, not a stale snapshot.
        assert paths.autoswitch_pause_file() == paths.Path("/b/.cswap-web-paused")

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


def _post_raw(port: int, body: bytes, content_length: str) -> int:
    """POST with a hand-written Content-Length, bypassing the client's own."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/api/threshold", skip_accept_encoding=True)
    conn.putheader("Host", "127.0.0.1")
    conn.putheader("Content-Type", "application/json")
    conn.putheader("Content-Length", content_length)
    conn.endheaders()
    conn.send(body)
    try:
        return conn.getresponse().status
    finally:
        conn.close()


class TestContentLengthIsBoundedBothWays:
    """A negative Content-Length cleared an upper-bound-only check, stayed
    truthy, and reached rfile.read(-1) -- "read to EOF" -- so the 64 KB cap was
    skipped entirely and the handler thread could block forever."""

    def test_negative_content_length_is_refused(self, live_server):
        port, service = live_server
        body = json.dumps({"value": 85}).encode()
        assert _post_raw(port, body, "-1") == 413
        assert service.threshold_calls == []

    def test_non_integer_content_length_is_refused_not_500(self, live_server):
        port, service = live_server
        assert _post_raw(port, b"{}", "abc") == 400
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
    def _post(self, port: int, payload: dict) -> int:
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
