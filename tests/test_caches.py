"""Tests for _caches.py: the registry of module-level caches."""

from __future__ import annotations

from collections import OrderedDict

import pytest

from bettermemory import _caches, origin


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A private copy of the registry for the test, so the clearers it
    registers do not stay registered for the rest of the session."""
    monkeypatch.setattr(_caches, "_CLEARERS", list(_caches._CLEARERS))


def test_clear_all_runs_a_registered_clearer(registry: None) -> None:
    calls: list[str] = []

    def clear() -> None:
        calls.append("cleared")

    assert _caches.register(clear) is clear
    _caches.clear_all()
    assert calls == ["cleared"]


def test_register_serves_as_a_decorator(registry: None) -> None:
    calls: list[int] = []

    @_caches.register
    def clear() -> None:
        calls.append(1)

    clear()
    _caches.clear_all()
    assert calls == [1, 1]


def test_a_clearer_registered_twice_runs_once(registry: None) -> None:
    calls: list[int] = []

    def clear() -> None:
        calls.append(1)

    _caches.register(clear)
    _caches.register(clear)
    _caches.clear_all()
    assert calls == [1]


def test_a_bound_method_read_twice_is_one_clearer(registry: None) -> None:
    """Every read of ``memo.clear`` makes a new bound-method object, equal
    to the last but not identical to it; the registry keeps it once."""
    memo: OrderedDict[str, int] = OrderedDict(key=1)
    assert memo.clear is not memo.clear
    _caches.register(memo.clear)
    _caches.register(memo.clear)
    assert _caches._CLEARERS.count(memo.clear) == 1
    _caches.clear_all()
    assert not memo


def test_clear_all_empties_the_reachable_walk_memo() -> None:
    """origin registers its reachable-walk memo's clearer when it is
    imported."""
    assert origin._clear_walk_memo in _caches._CLEARERS
    anchor = "a" * 40
    origin._WALK_MEMO[("/repo", anchor, anchor)] = origin.ReachableWalk(
        anchor=anchor, head=anchor, commits=(), touched={}
    )
    _caches.clear_all()
    assert not origin._WALK_MEMO
