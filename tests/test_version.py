"""Tests for the package version surface.

`bettermemory.__version__` is sourced from `importlib.metadata` so it
always matches `pyproject.toml`. The original 0.x-era code hard-coded a
literal that drifted past 1.0 — this guard prevents the same regression.

The argparse `--version` flag (registered in `server.main()`) reads the
same `__version__`, so the assertions on bare equality below cover both
surfaces in one go.
"""

from __future__ import annotations

import re
import subprocess
import sys
from importlib.metadata import version as pkg_version

import bettermemory


def test_dunder_version_matches_installed_metadata() -> None:
    """`bettermemory.__version__` reads from `importlib.metadata` so it
    can never drift from `pyproject.toml`. The fallback path
    (`0+unknown`) only fires when the package isn't installed at all,
    which never happens during the test suite — `pip install -e .` (or
    the editable install uv leaves behind) always registers metadata."""
    assert bettermemory.__version__ == pkg_version("bettermemory")


def test_dunder_version_looks_like_a_pep440_version() -> None:
    """Cheap shape check. PEP 440 has corner cases we don't need to
    parse exactly — this rejects the obvious failures (empty string,
    leftover placeholder, the legacy hard-coded `0.1.0` if it ever
    sneaks back)."""
    v = bettermemory.__version__
    assert v, "__version__ is empty"
    assert v != "0.1.0", (
        "__version__ matches the legacy hard-coded literal — the "
        "importlib.metadata source-of-truth was bypassed."
    )
    # Permissive PEP 440 shape: digits, dots, optional pre-release /
    # local-version markers. The fallback "0+unknown" matches too,
    # which is fine — that's the unrecognized-environment branch.
    assert re.match(r"^\d", v), f"__version__ should start with a digit: {v!r}"


def test_version_flag_prints_dunder_version() -> None:
    """The CLI's `--version` flag is the user-facing version surface;
    pin it to the same source. Spawning a subprocess catches argparse
    wiring that the in-process import test misses."""
    result = subprocess.run(
        [sys.executable, "-m", "bettermemory", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    # argparse `action="version"` writes to stdout and exits 0.
    out = (result.stdout + result.stderr).strip()
    assert bettermemory.__version__ in out, (
        f"--version output {out!r} does not contain {bettermemory.__version__!r}"
    )
    assert out.startswith("bettermemory "), (
        f"--version output should be prefixed with the program name: {out!r}"
    )
