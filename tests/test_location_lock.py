"""The per-location lock is not evicted while a thread has taken it (ENG-160).

`_location_lock` takes a location's lock from `_LOCKS` under a guard and
acquires it after the guard is released. When the table was full, the
eviction dropped every lock not locked, which included one a thread had
taken but not yet acquired: the next resolution of that location minted
another lock and ran beside the first. The lock here is gated between the
take and the acquire, so the window is held open without timing.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from bettermemory import location
from bettermemory.membership import INDETERMINATE, Location


class _Gated:
    """A lock whose acquire by ``with`` waits until the test lets it go."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.at_gate = threading.Event()
        self.go = threading.Event()

    def locked(self) -> bool:
        return self._lock.locked()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> _Gated:
        self.at_gate.set()
        assert self.go.wait(10)
        self._lock.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._lock.release()


class _Threading:
    """`threading` whose first `Lock()` is the gated one."""

    def __init__(self) -> None:
        self.gated: _Gated | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(threading, name)

    def Lock(self) -> Any:  # noqa: N802 - mirrors threading.Lock
        if self.gated is None:
            self.gated = _Gated()
            return self.gated
        return threading.Lock()


def _at(path: str) -> Location:
    return Location(machine="M-lock", cwd=path, state=INDETERMINATE, detail="test")


def test_a_taken_lock_is_not_evicted_before_it_is_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thread A takes location N's lock and stops before acquiring it; four
    other locations fill the table and evict. N's lock stays, and a second
    resolution of N waits for A instead of minting its own."""
    proxy = _Threading()
    monkeypatch.setattr(location, "threading", proxy)
    monkeypatch.setattr(location, "_LOCKS", {})
    monkeypatch.setattr(location, "CACHE_CAP", 1)
    entered_a = threading.Event()
    leave_a = threading.Event()
    entered_c = threading.Event()

    def resolve_a() -> None:
        with location._location_lock(_at("/n")):
            entered_a.set()
            assert leave_a.wait(10)

    def resolve_c() -> None:
        with location._location_lock(_at("/n")):
            entered_c.set()

    a = threading.Thread(target=resolve_a, daemon=True)
    c = threading.Thread(target=resolve_c, daemon=True)
    a.start()
    try:
        for _ in range(100):
            if proxy.gated is not None:
                break
            threading.Event().wait(0.01)
        gated = proxy.gated
        assert gated is not None and gated.at_gate.wait(10)
        for i in range(4):
            with location._location_lock(_at(f"/other{i}")):
                pass
        kept = "M-lock:/n" in location._LOCKS
        gated.go.set()
        assert entered_a.wait(10)
        c.start()
        overlapped = entered_c.wait(0.3)
    finally:
        if proxy.gated is not None:
            proxy.gated.go.set()
        leave_a.set()
        a.join(10)
        if c.ident is not None:
            c.join(10)
    assert not overlapped, "a second resolution of N ran beside the first"
    assert kept, "N's lock was evicted while a thread had taken it"
    assert entered_c.is_set()


def test_locks_no_thread_holds_are_evicted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The table stays bounded: locks no thread holds or waits on go when
    it is full, and a lock's users are released when its holder leaves."""
    monkeypatch.setattr(location, "_LOCKS", {})
    monkeypatch.setattr(location, "CACHE_CAP", 1)
    for i in range(12):
        with location._location_lock(_at(f"/p{i}")):
            pass
    assert len(location._LOCKS) <= 4
    held = location._LOCKS["M-lock:/p11"]
    assert getattr(held, "users", 0) == 0
