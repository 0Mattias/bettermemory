"""Shared pytest fixtures.

Each test gets a fresh temp memory directory so the tests stay hermetic.
"""

from __future__ import annotations

import importlib.util
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
