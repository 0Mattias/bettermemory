"""The registry of the package's module-level caches.

A module that keeps a cache at module level registers the function that
empties it when the module is imported, and `clear_all` runs every
registered function. The test suite calls it before each test
(``tests/conftest.py``), so no test starts from a value an earlier test
computed.

A cache belongs here only if it is exact: for the same inputs it returns
what the uncached code returns, and only the time changes. Emptying one is
then never a change in behaviour, only a cost.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

__all__ = ["clear_all", "register"]

_Clear = TypeVar("_Clear", bound=Callable[[], None])

_CLEARERS: list[Callable[[], None]] = []


def register(clear: _Clear) -> _Clear:
    """Add `clear` to the registry and return it unchanged, so it also
    serves as a decorator.

    A clearer registered twice is kept once. The comparison is equality,
    which is identity for a function and, for a bound method, the same
    function bound to the same object: ``memo.clear`` read twice gives two
    method objects, equal and not identical, and they register once.
    """
    if clear not in _CLEARERS:
        _CLEARERS.append(clear)
    return clear


def clear_all() -> None:
    """Run every registered clearer, in the order they were registered."""
    for clear in tuple(_CLEARERS):
        clear()
