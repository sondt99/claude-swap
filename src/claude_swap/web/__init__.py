"""Browser dashboard for claude-swap.

Entry point for ``cswap web``. The HTTP layer lives in :mod:`.server` and is
imported lazily inside :func:`run`, so the plain CLI paths — ``cswap list``,
cron's ``cswap auto --once`` — never pay for ``http.server`` or the frontend
stat, exactly as the TUI keeps textual out of those paths.

The dashboard is a *shell* over the same supported hooks the TUI uses
(``SnapshotSource.take``, ``run_action``, ``json_output.usage_to_json``); it
holds no switching, OAuth, or usage logic of its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claude_swap.switcher import ClaudeAccountSwitcher


def run(
    switcher: "ClaudeAccountSwitcher",
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    open_browser: bool = True,
    no_auth: bool = False,
) -> int:
    """Serve the dashboard over an existing switcher. Returns the exit code."""
    from claude_swap.web.server import run as _run

    return _run(
        switcher,
        host=host,
        port=port,
        open_browser=open_browser,
        no_auth=no_auth,
    )
