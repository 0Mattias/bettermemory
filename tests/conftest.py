"""Shared pytest fixtures.

Each test gets a fresh temp memory directory so the tests stay hermetic.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# Make `src/` importable without depending on the editable install. This is
# belt-and-suspenders: if the venv's editable .pth file is unreadable for any
# reason (macOS UF_HIDDEN propagation in iCloud-synced dirs, a stale env, a
# tooling bug), tests still pass.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import pytest

from bettermemory.config import BehaviorConfig, Config, ScopesConfig, StorageConfig
from bettermemory.session import SessionState
from bettermemory.store import Store

from ._event_helpers import EventLog


# ---------------------------------------------------------------------------
# Marker-driven autoskip for optional-extras tests.
#
# The `no_extras` family of markers (registered in pyproject.toml) tags tests
# that assert *the absence* of an optional dependency — e.g. "get_model
# returns None on ImportError". CI runs these in a job where the extra is
# explicitly NOT installed. Locally, a developer who has `sentence-transformers`
# or `fastembed` in their venv (typical, since they ship in the embeddings
# extras) would otherwise see these tests fail loudly. Auto-skipping when the
# extra IS importable keeps the local dev loop quiet without weakening CI.
# ---------------------------------------------------------------------------

_TORCH_PRESENT = importlib.util.find_spec("sentence_transformers") is not None
_FASTEMBED_PRESENT = importlib.util.find_spec("fastembed") is not None


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    skip_no_extras = pytest.mark.skip(
        reason="embeddings extra is installed — test asserts its absence"
    )
    skip_no_torch = pytest.mark.skip(
        reason="sentence-transformers is installed — test asserts its absence"
    )
    skip_no_fastembed = pytest.mark.skip(
        reason="fastembed is installed — test asserts its absence"
    )
    for item in items:
        if "no_extras" in item.keywords and (_TORCH_PRESENT or _FASTEMBED_PRESENT):
            item.add_marker(skip_no_extras)
        if "no_torch_embeddings" in item.keywords and _TORCH_PRESENT:
            item.add_marker(skip_no_torch)
        if "no_fastembed" in item.keywords and _FASTEMBED_PRESENT:
            item.add_marker(skip_no_fastembed)


def set_git_discovery_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin git's upward repo discovery inside the pytest sandbox.

    For tests whose premise is "this directory is NOT inside any git
    repo": whenever ``tmp_path`` itself sits under a real checkout (a
    TMPDIR redirected into one, ``--basetemp`` pointed inside one — CI
    images with repo-rooted temp, developers aiming basetemp at a
    workspace), git's upward walk escapes the fixture and finds the
    enclosing repo, silently flipping the premise: ``capture()`` reads
    that repo's branch/worktree_root and the commit-log primitives
    return its history instead of the None/fallback the tests assert.

    ``GIT_CEILING_DIRECTORIES`` stops the walk, but git honours a
    ceiling only as a STRICT ancestor of the probe directory
    (``longest_ancestor_length`` requires ``path[len] == '/'``;
    verified on git 2.50.1: a probe launched FROM the ceiling directory
    itself escapes upward unhindered). An entry AT the probe dir is
    therefore inert — so the helper pins ``tmp_path.parent`` AND
    ``tmp_path.parent.parent``, strict ancestors of every probe these
    tests launch (from ``tmp_path`` or below, and from
    ``tmp_path.parent`` for store-IS-tmp_path shapes), while any repo a
    test inits at or under ``tmp_path`` stays strictly below both
    ceilings and still resolves normally.

    Deliberately a plain per-test opt-in, NOT an autouse fixture:
    outside-repo premises are the exception, and a blanket ceiling
    could mask real discovery behaviour in tests that *want* the
    upward walk exercised.

    Test-side control ONLY: production code never sets this variable —
    a real user's own ``GIT_CEILING_DIRECTORIES`` is their
    configuration to keep.

    The doctor nested-store checks opened this hermeticity class and
    carried the original local twin of this helper (since folded in
    here): their upward walks append phantom levels to
    ``scanned_parent_toplevels`` when unpinned — removing the ceiling
    while pointing ``--basetemp`` inside a scratch git repo
    reintroduces exactly those failures.
    """
    monkeypatch.setenv(
        "GIT_CEILING_DIRECTORIES",
        os.pathsep.join([str(tmp_path.parent), str(tmp_path.parent.parent)]),
    )


@pytest.fixture
def memory_dir(tmp_path: Path) -> Path:
    d = tmp_path / "memories"
    d.mkdir()
    return d


@pytest.fixture
def store(memory_dir: Path) -> Store:
    return Store(memory_dir)


@pytest.fixture
def config(memory_dir: Path) -> Config:
    return Config(
        storage=StorageConfig(directory=str(memory_dir)),
        behavior=BehaviorConfig(),
        scopes=ScopesConfig(),
    )


@pytest.fixture
def session() -> SessionState:
    return SessionState()


@pytest.fixture
def event_log(tmp_path: Path) -> EventLog:
    """Real ``Recorder``-backed event log for event-consumer tests.

    Replaces hand-built event dict literals. The 2.6.2 and 2.6.3
    field-name bugs both shipped because tests used a dict shape that
    didn't match what production emits — see
    ``tests/_event_helpers.py`` for the full rationale.
    """
    return EventLog(root=tmp_path)
