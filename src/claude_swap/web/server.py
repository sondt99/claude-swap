"""Web shell for claude-swap — a browser dashboard over the same primitives the TUI uses.

claude-swap exposes three supported hooks for alternate front-ends (see
``snapshot_source.py``: "the supported read path for dashboards and GUI shells"):

    SnapshotSource.take()            -> one coherent AccountsSnapshot, store-paced
    run_action(partial(switcher.X))  -> captured mutation, returns ActionResult
    json_output.usage_to_json()      -> the maintained camelCase projection

This module is the HTTP layer over those; it contains no switching, OAuth, or
usage logic of its own.

Security posture — this process can switch and disable Claude credentials, so a
web page from any origin reaching it would be a real problem:

  * binds 127.0.0.1 only
  * a random per-run token, handed over in the launch URL, then pinned to a
    SameSite=Strict cookie
  * Origin/Referer rejected on every mutation unless it matches our own
  * Host header pinned to localhost (blocks DNS rebinding)
  * no CORS headers are ever emitted, so cross-origin reads stay blocked
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import secrets
import sys
import threading
import time
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from claude_swap import __version__ as CSWAP_VERSION
from claude_swap.exceptions import ClaudeSwitchError
from claude_swap.json_output import usage_to_json
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui.data import (
    SnapshotSource,
    format_age,
    run_action,
    sentinel_label,
)

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"

# The store gates real network calls per account, so polling this often costs
# nothing but a lock acquisition on most passes (see SnapshotSource docstring).
POLL_INTERVAL_S = 10.0
MAX_SSE_CLIENTS = 8

# One year. The token URL is meant to be opened once ever; after that the
# bookmark is the bare origin and the token is never seen again.
COOKIE_MAX_AGE_S = 31_536_000

HELP_PAGE = """<!doctype html>
<meta charset="utf-8"><title>cswap · token needed</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{color-scheme:light dark}
 body{margin:0;min-height:100vh;display:grid;place-items:center;
   font:14px/1.6 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;
   background:#f6f7f9;color:#14161a}
 @media(prefers-color-scheme:dark){body{background:#0e1014;color:#e8eaee}}
 .c{max-width:520px;padding:28px 30px;border-radius:14px;background:#fff;
   border:1px solid #e3e6ec;box-shadow:0 6px 24px rgba(16,20,30,.07);margin:20px}
 @media(prefers-color-scheme:dark){.c{background:#171a20;border-color:#2a2f38;
   box-shadow:0 6px 24px rgba(0,0,0,.3)}}
 h1{font-size:16px;margin:0 0 10px}
 p{margin:0 0 12px;color:#6b7280}
 @media(prefers-color-scheme:dark){p{color:#9aa1ad}}
 code{display:block;padding:10px 12px;border-radius:8px;background:#f1f3f7;
   font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
   overflow-x:auto;white-space:pre}
 @media(prefers-color-scheme:dark){code{background:#1c2027}}
 b{font-weight:600}
</style>
<div class="c">
  <h1>Cần token để mở dashboard</h1>
  <p>Lấy token trên máy bạn:</p>
  <code>grep CSWAP_WEB_TOKEN ~/cswap-web/.env</code>
  <p>Rồi mở <b>một lần duy nhất</b> với token đó:</p>
  <code>http://127.0.0.1:8787/?token=&lt;token&gt;</code>
  <p>Sau lần đó token được ghim vào cookie (1 năm) — từ đó vào thẳng
     <b>127.0.0.1:8787</b>, không cần token nữa.</p>
</div>
"""


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def account_to_json(acc) -> dict:
    """Project an AccountSnapshot for the browser.

    Usage goes through ``usage_to_json`` — the same projection ``cswap list
    --json`` emits — so countdowns/pace stay correct as the measurement ages
    and we inherit any upstream schema fix for free.
    """
    entry = acc.usage
    row = {
        "number": acc.number,
        "email": acc.email,
        "alias": acc.alias or "",
        "orgName": acc.org_name or "",
        "orgTag": acc.display_tag,
        "isActive": acc.is_active,
        "kind": acc.kind,
        "switchable": acc.switchable,
        "disabled": acc.disabled,
        "sentinel": entry.sentinel,
        "sentinelLabel": sentinel_label(entry.sentinel) if entry.sentinel else None,
        "usage": None,
        "ageText": format_age(entry.age_s),
        "ageSeconds": entry.age_s,
        # Fetch health, surfaced per account. A silent fetch outage is the
        # failure mode that actually hurts: the engine keeps deciding on
        # last-good usage while the real number runs away from it, and nothing
        # in a usage-only view looks wrong. (Lived it: a container missing its
        # CA bundle failed every fetch for three hours as a generic "network"
        # error, and the only symptom was late switching.)
        "lastError": entry.last_error,
        "failures": entry.consecutive_failures,
        "pollIntervalS": entry.poll_interval_s,
    }
    if isinstance(entry.last_good, dict):
        row["usage"] = usage_to_json(entry.last_good, entry.fetched_at)
    return row


# An account is only *expected* to be this stale: the slowest scheduled cadence
# in poll_policy is 600s (idle candidate / exhausted), plus jitter and a tick.
STALE_AFTER_S = 900.0

# Presence of this file holds the autoswitch engine off. A file rather than an
# in-process flag because the engine runs in a *different container*; both see
# it through the shared home mount. autoswitch-loop.sh checks it each tick.
PAUSE_FILE = Path.home() / ".cswap-web-paused"


def autoswitch_paused() -> bool:
    return PAUSE_FILE.exists()


def set_autoswitch_paused(paused: bool) -> None:
    if paused:
        PAUSE_FILE.touch(mode=0o600, exist_ok=True)
    else:
        PAUSE_FILE.unlink(missing_ok=True)


def snapshot_to_json(snap) -> dict:
    accounts = [account_to_json(a) for a in snap.accounts]
    ages = [a["ageSeconds"] for a in accounts if a["ageSeconds"] is not None]
    failing = [a for a in accounts if (a["failures"] or 0) >= 2]
    max_age = max(ages) if ages else None
    return {
        "activeNumber": snap.active_number,
        "takenAt": snap.taken_at,
        "serverTime": time.time(),
        "accounts": accounts,
        "autoswitchPaused": autoswitch_paused(),
        "health": {
            "degraded": bool(failing) or (max_age is not None and max_age > STALE_AFTER_S),
            "failingCount": len(failing),
            "maxAgeSeconds": max_age,
            "lastError": failing[0]["lastError"] if failing else None,
        },
    }


# ---------------------------------------------------------------------------
# Service — owns the switcher, serializes access, fans snapshots out over SSE
# ---------------------------------------------------------------------------


class Service:
    """Single owner of the switcher.

    Every switcher entry point here blocks on file locks, keychain subprocesses
    and network, and none of them are safe to run concurrently against the same
    live store — so one lock guards them all. Snapshots are taken by one poller
    thread and broadcast, so ten open tabs still cost one collect pass.
    """

    def __init__(
        self, switcher: ClaudeAccountSwitcher | None = None, debug: bool = False
    ) -> None:
        # The CLI already built one (and paid for its migrations/lock setup), so
        # reuse it the way the TUI and menu bar entry points do; constructing a
        # second against the same store would double that work for nothing.
        self.switcher = switcher if switcher is not None else ClaudeAccountSwitcher(debug=debug)
        self.source = SnapshotSource(self.switcher)
        self._lock = threading.Lock()
        self._latest: dict | None = None
        self._subscribers: list[queue.Queue] = []
        self._sub_lock = threading.Lock()
        self._wake = threading.Event()

    # -- snapshots ----------------------------------------------------------

    def refresh(self, full: bool = False) -> dict:
        with self._lock:
            snap = self.source.take(full=full)
        payload = snapshot_to_json(snap)
        self._latest = payload
        self._broadcast(payload)
        return payload

    def latest(self) -> dict:
        return self._latest if self._latest is not None else self.refresh()

    def poll_loop(self) -> None:
        while True:
            try:
                self.refresh()
            except Exception as e:  # a dashboard must not die on one bad pass
                self._broadcast({"error": f"{type(e).__name__}: {e}"})
            self._wake.wait(POLL_INTERVAL_S)
            self._wake.clear()

    def nudge(self) -> None:
        """Ask the poller to take a fresh pass now (after a mutation)."""
        self._wake.set()

    # -- SSE fan-out --------------------------------------------------------

    def subscribe(self) -> queue.Queue | None:
        with self._sub_lock:
            if len(self._subscribers) >= MAX_SSE_CLIENTS:
                return None
            q: queue.Queue = queue.Queue(maxsize=4)
            self._subscribers.append(q)
            return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def _broadcast(self, payload: dict) -> None:
        with self._sub_lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass  # slow tab; it will catch up on the next tick

    # -- actions ------------------------------------------------------------

    def act(self, fn) -> dict:
        """Run a switcher mutation captured, then republish the world."""
        with self._lock:
            result = run_action(fn)
        out = {
            "ok": result.ok,
            "message": result.first_line,
            "output": result.output,
            "payload": result.payload,
        }
        self.nudge()
        return out

    def switch_to(self, identifier: str) -> dict:
        return self.act(partial(self.switcher.switch_to, identifier, json_output=True))

    def switch_strategy(self, strategy: str | None) -> dict:
        return self.act(
            partial(self.switcher.switch, strategy=strategy, json_output=True)
        )

    def set_disabled(self, identifier: str, disabled: bool) -> dict:
        return self.act(
            partial(self.switcher.set_account_disabled, identifier, disabled)
        )

    def set_alias(self, identifier: str, alias: str) -> dict:
        return self.act(partial(self.switcher.set_alias, identifier, alias))


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "cswap-web"
    protocol_version = "HTTP/1.1"

    service: Service
    token: str
    port: int
    no_auth: bool

    # -- guards -------------------------------------------------------------

    def _host_ok(self) -> bool:
        """Pin Host to loopback so a rebound DNS name cannot drive this."""
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _origin_ok(self) -> bool:
        """Reject any cross-site caller on mutations.

        A missing Origin is allowed (curl, same-origin GET); a present one must
        be ours. Referer is checked the same way as a second line.
        """
        allowed = {
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        }
        origin = self.headers.get("Origin")
        if origin is not None and origin not in allowed:
            return False
        referer = self.headers.get("Referer")
        if referer:
            parsed = urlparse(referer)
            if f"{parsed.scheme}://{parsed.netloc}" not in allowed:
                return False
        return True

    def _authed(self, query: dict) -> bool:
        # Auth off: the remaining defenses still stand, and they are the ones
        # that matter against the web. A browser cannot forge a same-origin
        # POST here — cross-origin simple requests carry Origin (rejected
        # below), and anything with a JSON content type is preflighted, which
        # this server never answers. What auth-off *does* concede is local
        # processes: any code on this machine can now drive a switch.
        if self.no_auth:
            return True
        supplied = None
        if "token" in query:
            supplied = query["token"][0]
        else:
            cookie = self.headers.get("Cookie") or ""
            for part in cookie.split(";"):
                name, _, value = part.strip().partition("=")
                if name == "cswap_token":
                    supplied = value
                    break
        header = self.headers.get("X-Cswap-Token")
        if supplied is None and header:
            supplied = header
        return supplied is not None and secrets.compare_digest(supplied, self.token)

    # -- replies ------------------------------------------------------------

    def _send(self, status, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: dict, status=HTTPStatus.OK) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _deny(self, status, message: str) -> None:
        self._send(status, message.encode(), "text/plain; charset=utf-8")

    # -- routing ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._deny(HTTPStatus.FORBIDDEN, "bad host")
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path

        if route == "/healthz":
            return self._json({"ok": True, "cswap": CSWAP_VERSION})

        if not self._authed(query):
            # A browser hitting the bare URL is the common case (an expired or
            # never-set cookie), and a bare "missing or bad token" gives the
            # user nowhere to go — so tell them where the token lives. Never
            # echo the token itself: this response is unauthenticated.
            if route == "/":
                return self._send(
                    HTTPStatus.UNAUTHORIZED,
                    HELP_PAGE.encode(),
                    "text/html; charset=utf-8",
                )
            return self._deny(HTTPStatus.UNAUTHORIZED, "missing or bad token")

        if route == "/":
            body = INDEX.read_bytes()
            if self.no_auth:
                return self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
            # Pin the token to a cookie so it never has to ride in fetch URLs
            # (and never lands in the address bar on reload). A year, not a
            # day: the point is that the token URL is opened exactly once, so
            # the bookmark is the bare origin from then on.
            cookie = (
                f"cswap_token={self.token}; Path=/; HttpOnly; "
                f"SameSite=Strict; Max-Age={COOKIE_MAX_AGE_S}"
            )
            return self._send(
                HTTPStatus.OK, body, "text/html; charset=utf-8", {"Set-Cookie": cookie}
            )

        if route == "/api/snapshot":
            try:
                return self._json(self.service.latest())
            except ClaudeSwitchError as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

        if route == "/api/events":
            return self._sse()

        return self._deny(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._deny(HTTPStatus.FORBIDDEN, "bad host")
        if not self._origin_ok():
            return self._deny(HTTPStatus.FORBIDDEN, "cross-site request refused")
        parsed = urlparse(self.path)
        if not self._authed(parse_qs(parsed.query)):
            return self._deny(HTTPStatus.UNAUTHORIZED, "missing or bad token")

        length = int(self.headers.get("Content-Length") or 0)
        if length > 64_000:
            return self._deny(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return self._json({"ok": False, "message": "bad JSON"}, HTTPStatus.BAD_REQUEST)
        if not isinstance(body, dict):
            return self._json({"ok": False, "message": "bad body"}, HTTPStatus.BAD_REQUEST)

        try:
            result = self._dispatch(parsed.path, body)
        except ClaudeSwitchError as e:
            return self._json({"ok": False, "message": str(e)}, HTTPStatus.BAD_REQUEST)
        except ValueError as e:
            return self._json({"ok": False, "message": str(e)}, HTTPStatus.BAD_REQUEST)
        if result is None:
            return self._deny(HTTPStatus.NOT_FOUND, "not found")
        return self._json(result)

    def _dispatch(self, route: str, body: dict) -> dict | None:
        svc = self.service
        if route == "/api/switch":
            identifier = str(body.get("identifier") or "").strip()
            if not identifier:
                raise ValueError("identifier is required")
            return svc.switch_to(identifier)
        if route == "/api/switch-strategy":
            strategy = body.get("strategy")
            if strategy not in (None, "best", "next-available"):
                raise ValueError(f"unknown strategy: {strategy}")
            return svc.switch_strategy(strategy)
        if route == "/api/disabled":
            identifier = str(body.get("identifier") or "").strip()
            if not identifier:
                raise ValueError("identifier is required")
            return svc.set_disabled(identifier, bool(body.get("disabled")))
        if route == "/api/alias":
            identifier = str(body.get("identifier") or "").strip()
            alias = str(body.get("alias") or "").strip()
            if not identifier or not alias:
                raise ValueError("identifier and alias are required")
            return svc.set_alias(identifier, alias)
        if route == "/api/autoswitch":
            paused = bool(body.get("paused"))
            set_autoswitch_paused(paused)
            svc.nudge()
            return {
                "ok": True,
                "message": (
                    "Autoswitch đã tạm dừng — lựa chọn tay của bạn sẽ đứng yên"
                    if paused
                    else "Autoswitch đã bật lại"
                ),
                "output": "",
                "payload": {"paused": paused},
            }
        if route == "/api/refresh":
            return {"ok": True, "message": "refreshed", "output": "", "payload": None}
        return None

    # -- SSE ----------------------------------------------------------------

    def _sse(self) -> None:
        q = self.service.subscribe()
        if q is None:
            return self._deny(HTTPStatus.SERVICE_UNAVAILABLE, "too many listeners")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self._sse_write(self.service.latest())
            while True:
                try:
                    payload = q.get(timeout=20.0)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")  # hold the connection open
                    self.wfile.flush()
                    continue
                self._sse_write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.service.unsubscribe(q)

    def _sse_write(self, payload: dict) -> None:
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # the request log would leak the token on the initial GET


def run(
    switcher: ClaudeAccountSwitcher | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
    no_auth: bool = False,
    debug: bool = False,
) -> int:
    """Serve the dashboard until interrupted. The ``cswap web`` entry point.

    ``switcher`` is the CLI's already-constructed instance; passing None builds
    one, which is what the standalone ``python -m claude_swap.web.server`` path
    does.
    """
    if not INDEX.exists():
        print(f"missing frontend: {INDEX}", file=sys.stderr)
        return 1

    # A long-lived service needs a stable URL, so an operator-supplied token is
    # honored; interactive runs get a fresh random one per launch.
    token = os.environ.get("CSWAP_WEB_TOKEN") or secrets.token_urlsafe(24)
    service = Service(switcher, debug=debug)

    handler = type(
        "BoundHandler",
        (Handler,),
        {
            "service": service,
            "token": token,
            "port": port,
            "no_auth": no_auth,
        },
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True

    threading.Thread(target=service.poll_loop, daemon=True).start()

    url = (
        f"http://127.0.0.1:{port}/"
        if no_auth
        else f"http://127.0.0.1:{port}/?token={token}"
    )
    # flush=True: stdout is block-buffered whenever it isn't a tty, so a
    # redirected or service-managed run would otherwise never show this —
    # and the token is only ever printed here.
    print(f"cswap-web  ·  claude-swap {CSWAP_VERSION}", flush=True)
    print(f"  {url}", flush=True)
    if no_auth:
        # Stated plainly on every start: an unauthenticated switch endpoint is
        # easy to forget about months later.
        print(
            "  AUTH DISABLED — any local process can switch/disable your "
            "accounts.\n"
            "  Still enforced: loopback bind, Origin check, Host pin, no CORS.",
            flush=True,
        )
    print("  Ctrl-C to stop", flush=True)
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


def main() -> int:
    """Standalone entry point, kept so the module still runs on its own."""
    ap = argparse.ArgumentParser(description="Web dashboard for claude-swap")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; 0.0.0.0 only inside a container whose port is "
        "published to 127.0.0.1 on the host",
    )
    ap.add_argument("--no-browser", action="store_true", help="don't auto-open")
    ap.add_argument("--debug", action="store_true", help="cswap debug logging")
    ap.add_argument(
        "--no-auth",
        action="store_true",
        default=os.environ.get("CSWAP_WEB_NO_AUTH") == "1",
        help="serve without a token (env: CSWAP_WEB_NO_AUTH=1). Loopback bind, "
        "Origin and Host checks still apply, but any local process can then "
        "drive a switch.",
    )
    args = ap.parse_args()
    return run(
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        no_auth=args.no_auth,
        debug=args.debug,
    )


if __name__ == "__main__":
    raise SystemExit(main())
