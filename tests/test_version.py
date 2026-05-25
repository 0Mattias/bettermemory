"""Tests for the package version surface.

`bettermemory.__version__` is sourced from `importlib.metadata` so it
always matches `pyproject.toml`. The original 0.x-era code hard-coded a
literal that drifted past 1.0 — this guard prevents the same regression.

The argparse `--version` flag (registered in `server.main()`) reads the
same `__version__`, so the assertions on bare equality below cover both
surfaces in one go.

The H12 audit pass (2.7.x) caught a stale local install where
`bettermemory --version` reported 2.7.2 while pyproject said 2.7.3.
The CI gate added at the bottom of this file pins all FOUR version
sources together so the same drift can't slip through to a release
wheel: `pyproject.toml`, `bettermemory.__version__`, the CLI
subprocess output, and the plugin/marketplace manifests (the
plugin pair is already pinned in `test_plugin.py` /
`test_changelog.py` — re-asserting here in one consolidated test
makes the version-skew failure mode legible from a single grep).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from importlib.metadata import version as pkg_version
from pathlib import Path

import pytest

import bettermemory


_REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with (_REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


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


# `python -m bettermemory` only resolves when the package is installed
# in the subprocess Python — `pip install -e .` or a wheel install. CI
# does this via `uv sync --extra dev`; a fresh local clone without the
# editable install would otherwise see this fail with "No module named
# bettermemory". Skip when the subprocess can't import.
_PACKAGE_IMPORTABLE_IN_SUBPROCESS = (
    subprocess.run(
        [sys.executable, "-c", "import bettermemory"],
        capture_output=True,
    ).returncode
    == 0
)


@pytest.mark.skipif(
    not _PACKAGE_IMPORTABLE_IN_SUBPROCESS,
    reason=(
        "subprocess Python can't import bettermemory — "
        "run `pip install -e .` (or `uv sync`) locally"
    ),
)
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


# ---------------------------------------------------------------------------
# H12 — version skew across ALL the surfaces a release tag has to keep
# in lockstep. Each source pinned to pyproject.toml so a release with
# a bumped pyproject but a stale anywhere-else surfaces here.
# ---------------------------------------------------------------------------


def test_pyproject_version_matches_dunder_version() -> None:
    """`importlib.metadata` reads the installed wheel's metadata, which
    derives from pyproject at build time. A drift between this and
    pyproject means the local install is stale — `pip install -e .` (or
    `uv sync`) fixes it. The CI gate fires when the editable install on
    a release-tagged commit happens to be stale, so the audit-pass
    finding from 2.7.x can't repeat against a published wheel."""
    pyproject_v = _pyproject_version()
    assert bettermemory.__version__ == pyproject_v, (
        f"bettermemory.__version__ ({bettermemory.__version__!r}) != "
        f"pyproject.toml version ({pyproject_v!r}). The installed "
        f"package is stale relative to the source tree — run "
        f"`uv pip install -e .` (or `uv sync`) to refresh."
    )


@pytest.mark.skipif(
    not _PACKAGE_IMPORTABLE_IN_SUBPROCESS,
    reason=(
        "subprocess Python can't import bettermemory — "
        "run `pip install -e .` (or `uv sync`) locally"
    ),
)
def test_cli_version_flag_matches_pyproject() -> None:
    """The user-visible `bettermemory --version` output must match
    pyproject. This is the surface the H12 audit caught (2.7.2 wheel
    vs 2.7.3 pyproject) — pin it directly rather than indirectly
    through `__version__` so the drift fails loudly here even if
    `__version__` and pyproject agree but the CLI entry point was
    somehow served from a different process tree."""
    pyproject_v = _pyproject_version()
    result = subprocess.run(
        [sys.executable, "-m", "bettermemory", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    out = (result.stdout + result.stderr).strip()
    assert pyproject_v in out, (
        f"`bettermemory --version` output {out!r} does not contain the "
        f"pyproject.toml version {pyproject_v!r}. Most likely cause: a "
        f"stale editable install; run `uv pip install -e .`. If you "
        f"see this in CI, the release wheel is mis-tagged."
    )


def test_pyproject_matches_plugin_and_marketplace_manifests() -> None:
    """Re-pin the plugin + marketplace versions to pyproject here so a
    single `pytest tests/test_version.py` run surfaces every version-
    skew failure mode in one place. The detailed plugin-shape
    assertions still live in `test_plugin.py`; this is the
    consolidated guard."""
    pyproject_v = _pyproject_version()
    plugin = json.loads(
        (_REPO_ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(
            encoding="utf-8"
        )
    )
    market = json.loads(
        (_REPO_ROOT / ".claude-plugin" / "marketplace.json").read_text(
            encoding="utf-8"
        )
    )
    assert plugin.get("version") == pyproject_v, (
        f"plugin.json version {plugin.get('version')!r} != "
        f"pyproject.toml {pyproject_v!r} — bump both at release."
    )
    market_v = (market.get("metadata") or {}).get("version")
    assert market_v == pyproject_v, (
        f"marketplace.json metadata.version {market_v!r} != "
        f"pyproject.toml {pyproject_v!r} — bump both at release."
    )
