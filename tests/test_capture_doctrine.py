"""Capture uses the chat's own model or nothing (the owner's ruling of
2026-09-25): no capture path imports a model SDK or a network client,
reads an API key, or opens a socket of its own. The daemon binds
127.0.0.1 only, and capture reaches the model solely through the Stop
hook's reply on that one loopback connection."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src" / "bettermemory"
CAPTURE_PATHS = (
    "capture.py",
    "capture_hook.py",
    "capture_review.py",
    "cli/capture_cmd.py",
)
FORBIDDEN_MODULES = {
    "anthropic",
    "openai",
    "httpx",
    "httpx2",
    "requests",
    "urllib",
    "http",
    "socket",
    "aiohttp",
    "litellm",
    "google",
}


def _imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


@pytest.mark.parametrize("name", CAPTURE_PATHS)
def test_no_capture_path_reaches_a_model_a_key_or_the_network(name: str) -> None:
    source = (_SRC / name).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not _imports(tree) & FORBIDDEN_MODULES, name
    # No environment reads at all: a key, a provider or the attended mark
    # read from the daemon's own environment would each break the doctrine.
    for node in ast.walk(tree):
        named = getattr(node, "attr", None) or getattr(node, "id", None)
        assert named not in {"environ", "getenv", "putenv"}, (name, named)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "api_key" not in node.value.casefold(), name


def test_the_daemon_binds_loopback_only() -> None:
    from bettermemory._daemon_client import HOST

    assert HOST == "127.0.0.1"
