"""Check PyPI for newer versions of claude-swap."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from claude_swap.cache import CACHE_DIR, MISSING, read_cache, write_cache

CACHE_PATH = CACHE_DIR / "update_check.json"
CACHE_TTL = 24 * 3600  # 24 hours
PYPI_URL = "https://pypi.org/pypi/claude-swap/json"


def _parse_version(v: str) -> tuple:
    """Compare-able key: (release numbers, 1 if a final release else 0).

    A PEP 440 local version (``0.26.0b1+web.1``, what a self-built fork
    carries) or a pre-release suffix used to raise ValueError here, and the
    caller swallows that — so the builds most likely to be behind were the ones
    permanently told nothing.

    The trailing flag matters as much as the numbers: without it a pre-release
    compares EQUAL to its own final, so someone on ``0.26.0b1`` would never be
    told that ``0.26.0`` had shipped — the exact case this is meant to fix.
    """
    release = v.split("+", 1)[0]
    parts: list[int] = []
    is_final = 1
    for chunk in release.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
        if digits != chunk:
            # A trailing non-digit is a pre/post-release marker (b1, rc1).
            is_final = 0
            break
    if not parts:
        raise ValueError(f"no numeric release segment in {v!r}")
    return (tuple(parts), is_final)


def _is_prerelease(v: str) -> bool:
    """True for a PyPI version no plain upgrade command would install.

    ``uv tool upgrade`` and ``pipx upgrade`` both skip pre-releases without an
    explicit opt-in, so announcing one produces a banner the user cannot act
    on, re-shown every 24h forever.
    """
    return _parse_version(v)[1] == 0


def _detect_install_method() -> str | None:
    """Return 'uv', 'pipx', or None if we can't tell."""
    prefix = Path(sys.prefix)
    parts = tuple(p.lower() for p in prefix.parts)
    pairs = list(zip(parts, parts[1:]))

    if ("uv", "tools") in pairs:
        return "uv"
    if ("pipx", "venvs") in pairs:
        return "pipx"

    # Env-var override: only trust if sys.prefix is actually under it.
    for env_var, name in (("UV_TOOL_DIR", "uv"), ("PIPX_HOME", "pipx")):
        root = os.environ.get(env_var)
        if root:
            try:
                if prefix.is_relative_to(Path(root)):
                    return name
            except (ValueError, OSError):
                pass
    return None


def check_for_update(current_version: str) -> str | None:
    """Return a notification string if a newer version exists, else None."""
    try:
        latest_version = None

        # Try reading cache
        cached_data = read_cache(CACHE_PATH, CACHE_TTL)
        if cached_data is not MISSING:
            latest_version = cached_data
        else:
            # Fetch from PyPI
            try:
                req = urllib.request.Request(PYPI_URL)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    data = json.loads(resp.read().decode())
                latest_version = data["info"]["version"]
            except Exception:
                latest_version = None

            # Write cache regardless of success/failure
            write_cache(CACHE_PATH, latest_version)

        # A pre-release on PyPI is skipped: neither `uv tool upgrade` nor
        # `pipx upgrade` installs one without an explicit opt-in, so announcing
        # it yields a banner the user cannot act on, repeated every 24h.
        if (
            latest_version
            and not _is_prerelease(latest_version)
            and _parse_version(latest_version) > _parse_version(current_version)
        ):
            method = _detect_install_method()
            direct = {
                "uv": "uv tool upgrade claude-swap",
                "pipx": "pipx upgrade claude-swap",
            }.get(method or "")
            if direct and sys.platform != "win32":
                # cswap upgrade actually performs the upgrade here.
                hint = "Run `cswap upgrade` to update."
            elif direct:
                # Windows: cswap upgrade only prints, so point at the real command.
                hint = f"Run `{direct}` to update."
            else:
                # Unknown install method: cswap upgrade shows manual instructions.
                hint = "Run `cswap upgrade` for upgrade instructions."
            return (
                f"A newer version of claude-swap is available ({latest_version}). "
                f"You are using {current_version}. {hint}"
            )
        return None
    except Exception:
        return None


def run_self_upgrade() -> int:
    """Run the appropriate upgrade command for the current install method.

    Returns the subprocess exit code, or 1 if detection failed or the package
    manager is missing from PATH.
    """
    from claude_swap.printer import accent, error

    method = _detect_install_method()
    commands = {
        "uv": ["uv", "tool", "upgrade", "claude-swap"],
        "pipx": ["pipx", "upgrade", "claude-swap"],
    }
    cmd = commands.get(method or "")
    if cmd is None:
        error(
            "Could not detect install method (looked for uv tool / pipx).\n"
            f"  sys.prefix:     {sys.prefix}\n"
            f"  sys.executable: {sys.executable}\n"
            "To upgrade manually, run one of:\n"
            "  uv tool upgrade claude-swap\n"
            "  pipx upgrade claude-swap\n"
            f"  {sys.executable} -m pip install --upgrade claude-swap\n"
            "If you installed with `pip install -e .`, use `git pull` instead."
        )
        return 1

    # Windows: the running cswap.exe launcher is locked, so an in-process
    # uv/pipx upgrade fails when it tries to replace the executable even
    # though the package itself updates. cswap exits right after this, which
    # releases the lock, so the user can just run the command themselves.
    if sys.platform == "win32":
        print(f"To upgrade claude-swap on Windows, run:\n  {accent(' '.join(cmd))}")
        return 1

    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except FileNotFoundError:
        error(
            f"Detected {method} install but `{cmd[0]}` is not on PATH. "
            "Run the upgrade manually from a shell where it is available."
        )
        return 1
