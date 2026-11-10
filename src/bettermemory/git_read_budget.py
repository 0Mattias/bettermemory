"""One shared, request-local deadline for commit-history reads.

Only readers activate it: a deadline must never interrupt membership or a
write and turn an unanswered identity probe into an identity decision.
The exception unwinds a partial read before its failure can be memoised.
The caller keeps a distinct unread cause, and later calls start anew.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from threading import RLock
from time import monotonic as _clock
from typing import Any, ParamSpec, TypeVar, cast
from weakref import WeakKeyDictionary

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T")

# Match one history probe's existing ceiling, shared across the call.
READ_SECONDS = 5.0


class ReadDeadline(RuntimeError):
    """This call has no Git-read time left; it supplies no clean count."""


@dataclass
class _Progress:
    resume: tuple[str, bool] | None = None
    lock: RLock = field(default_factory=RLock)


@dataclass
class ReadBudget:
    progress: _Progress | None = None
    resume: tuple[str, bool] | None = None
    deadline: float | None = None
    first_home: str | None = None
    last_root: tuple[str, bool] | None = None
    deferred: tuple[str, bool] | None = None

    def allowance(self, requested: float, root: str, home: bool) -> tuple[float, bool]:
        now = _clock()
        if self.deadline is None:
            self.deadline = now + READ_SECONDS
        remaining = self.deadline - now
        self.last_root = (root, home)
        if remaining <= 0:
            self.exhausted()
        if home and self.first_home is None:
            self.first_home = root
        return min(requested, remaining), remaining < requested

    def exhausted(self) -> None:
        if self.deferred is None and self.last_root is not None:
            root, home = self.last_root
            if home:
                # Retry a partly reached later root first. A first home
                # that spends the whole call must not starve every other.
                self.deferred = (root, root == self.first_home)
        raise ReadDeadline()

    def finish(self) -> None:
        if self.progress is not None:
            with self.progress.lock:
                self.progress.resume = self.deferred


_OWNER: ContextVar[ReadBudget | None] = ContextVar("commit_read_owner", default=None)
_ACTIVE: ContextVar[ReadBudget | None] = ContextVar("commit_read_active", default=None)
_ROOT: ContextVar[tuple[str, bool] | None] = ContextVar(
    "commit_read_root", default=None
)
_PROGRESS: WeakKeyDictionary[Any, dict[str, _Progress]] = WeakKeyDictionary()
_LOCK = RLock()


def _progress(store: Any, name: str) -> _Progress | None:
    if store is None:
        return None
    with _LOCK:
        try:
            entries = _PROGRESS.setdefault(store, {})
            return entries.setdefault(name, _Progress())
        except TypeError:
            return None


@contextmanager
def call_scope(store: Any = None, name: str = "") -> Iterator[ReadBudget]:
    existing = _OWNER.get()
    if existing is not None:
        yield existing
        return
    progress = _progress(store, name)
    resume = None
    if progress is not None:
        with progress.lock:
            resume = progress.resume
    budget = ReadBudget(progress=progress, resume=resume)
    token = _OWNER.set(budget)
    try:
        yield budget
    finally:
        _OWNER.reset(token)
        budget.finish()


def call(fn: Callable[P, R]) -> Callable[P, R]:
    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = getattr(args[0], "store", None) if args else None
        with call_scope(store, fn.__module__ + "." + fn.__qualname__):
            return fn(*args, **kwargs)

    return wrapped


def health_call(fn: Callable[P, R]) -> Callable[P, R]:
    """Capped daemon health shares a deadline; an offline audit visits all roots."""

    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        if kwargs.get("estate_root_limit", 8) is None:
            return fn(*args, **kwargs)
        with call_scope():
            return fn(*args, **kwargs)

    return wrapped


@contextmanager
def reading(root: object, *, home: bool = True) -> Iterator[ReadBudget]:
    """Bound a whole root read, including its read-only Git identification."""
    budget = _ACTIVE.get() or _OWNER.get() or ReadBudget()
    active = _ACTIVE.set(budget)
    root_token = _ROOT.set((str(root), home))
    try:
        yield budget
    finally:
        _ROOT.reset(root_token)
        _ACTIVE.reset(active)


def expired() -> bool:
    """Whether the owner's started deadline has passed, without starting it."""
    budget = _ACTIVE.get() or _OWNER.get()
    return bool(
        budget is not None
        and budget.deadline is not None
        and _clock() >= budget.deadline
    )


@contextmanager
def wait_for(lock: RLock) -> Iterator[bool]:
    """A health progress lock spends the same deadline as its Git reads."""
    budget = _OWNER.get()
    if budget is None:
        acquired = lock.acquire()
    else:
        now = _clock()
        if budget.deadline is None:
            budget.deadline = now + READ_SECONDS
        remaining = budget.deadline - now
        acquired = remaining > 0 and lock.acquire(timeout=remaining)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def async_call(
    fn: Callable[P, Coroutine[Any, Any, R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    @wraps(fn)
    async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        store = getattr(args[0], "store", None) if args else None
        with call_scope(store, fn.__module__ + "." + fn.__qualname__):
            return await fn(*args, **kwargs)

    return wrapped


def _unread() -> Any:
    from .origin_history import HistoryUnread

    return HistoryUnread.CALL_BUDGET


def _read(fn: Callable[P, R], fallback: Callable[[], R]) -> Callable[P, R]:
    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        budget = _ACTIVE.get() or _OWNER.get() or ReadBudget()
        active = _ACTIVE.set(budget)
        root_token = None
        if args and hasattr(args[0], "at_home") and hasattr(args[0], "cwd"):
            root_token = _ROOT.set(
                (str(getattr(args[0], "cwd")), bool(getattr(args[0], "at_home")))
            )
        try:
            return fn(*args, **kwargs)
        except ReadDeadline:
            return fallback()
        finally:
            if root_token is not None:
                _ROOT.reset(root_token)
            _ACTIVE.reset(active)

    return wrapped


def unread(fn: Callable[P, R]) -> Callable[P, R]:
    """For a reader whose declared result already includes HistoryUnread."""
    return _read(fn, lambda: cast(R, _unread()))


def hit(fn: Callable[P, tuple[bool, R]]) -> Callable[P, tuple[bool, R]]:
    """For the answered flag and count/unread result of a hit lookup."""
    return _read(fn, lambda: (True, cast(R, _unread())))


def allowance(timeout: float, cwd: object, git_dir: str | None) -> tuple[float, bool]:
    budget = _ACTIVE.get()
    if budget is None:
        return timeout, False
    root, home = _ROOT.get() or (str(cwd if cwd is not None else git_dir), True)
    return budget.allowance(timeout, root, home)


def exhausted() -> None:
    budget = _ACTIVE.get()
    assert budget is not None
    budget.exhausted()


def for_resume(rows: Sequence[T], root: Callable[[T], str | None]) -> list[T]:
    """Rotate read execution after a deferred call, leaving output order intact."""
    budget = _OWNER.get()
    if budget is None or budget.resume is None:
        return list(rows)
    wanted, after = budget.resume
    indexes = [i for i, row in enumerate(rows) if root(row) == wanted]
    if not indexes:
        return list(rows)
    start = indexes[-1] + 1 if after else indexes[0]
    return [*rows[start:], *rows[:start]]
