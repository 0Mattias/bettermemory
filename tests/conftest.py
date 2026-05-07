"""Shared pytest fixtures.

Each test gets a fresh temp memory directory so the tests stay hermetic.
"""

from __future__ import annotations

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

from memory_mcp.config import BehaviorConfig, Config, ScopesConfig, StorageConfig
from memory_mcp.session import SessionState
from memory_mcp.store import Store


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
