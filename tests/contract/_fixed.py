"""The clocks and mints the record writers read, fixed for a generator.

`fixed` patches, for the length of a ``with`` block, every clock and mint a
record writer reaches, so a generator drives the reference
deterministically:

- the log's clock, ``bettermemory.log._utcnow_iso`` (a row's ``ts`` and the
  head file's ``ts``): `Ticker` ``log``;
- the wall clock, ``datetime.now`` as ``bettermemory.store_returned``
  (a ``returned`` mark's stamp), ``bettermemory.store_records`` (a
  standing answer's stamp, a removal's time) and ``bettermemory.store``
  (``created``, a home's ``decided``) read it, and ``utcnow`` as
  ``bettermemory.store_records`` and ``bettermemory.store_journal`` import
  it from ``bettermemory.models``: `Ticker` ``wall``;
- the ULID mint, ``generate_ulid`` as ``bettermemory.store_records`` and
  ``bettermemory.store_journal`` import it: ``mint`` (ULIDs numbered from 0
  unless given);
- when given, ``time.monotonic`` and ``time.sleep`` as ``bettermemory.store``
  reads them (`Store._in_chunks` cutting a long run into chunks, and its
  pause between them): ``monotonic`` and ``sleep``.

The two clocks are set apart, so a fixture can tell which one stamped a
value. Each `Ticker` keeps what it handed out (``read``), for the fixture
to record and a native test to replay. Everything is put back on exit;
no environment variable is set.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
import time as _real_time
from typing import Any
from unittest.mock import patch

from bettermemory import log as blog
from bettermemory import store as bstore
from bettermemory import store_journal, store_records, store_returned
from bettermemory.time_utils import isoformat_utc


class Ticker:
    """A clock that reads ``start``, then ``start + step``, and so on."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self.start = start
        self.step = step
        self.read: list[datetime] = []

    def __call__(self) -> datetime:
        now = self.start + self.step * len(self.read)
        self.read.append(now)
        return now

    def stamps(self) -> list[str]:
        """What was read, as ``isoformat_utc`` writes it."""
        return [isoformat_utc(t) for t in self.read]


def ulid_mint(start: int = 0) -> Callable[[], str]:
    """ULIDs numbered from `start`, under the prefix ``01M``."""
    digits = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    count = [start]

    def mint() -> str:
        n = count[0]
        count[0] += 1
        tail = ""
        for _ in range(23):
            tail = digits[n % 32] + tail
            n //= 32
        return "01M" + tail

    return mint


def _wall_datetime(wall: Ticker) -> type[datetime]:
    class _Wall(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return wall()

    return _Wall


class _Time:
    """``bettermemory.store``'s ``time`` with ``monotonic`` and ``sleep``
    replaced; every other name is the real module's."""

    def __init__(
        self, monotonic: Callable[[], float], sleep: Callable[[float], None]
    ) -> None:
        self.monotonic = monotonic
        self.sleep = sleep

    def __getattr__(self, name: str) -> Any:
        return getattr(_real_time, name)


@contextmanager
def fixed(
    *,
    log: Ticker,
    wall: Ticker,
    mint: Callable[[], str] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> Iterator[None]:
    """Every clock and mint of the record writers fixed (module docstring)."""
    mint = mint or ulid_mint()
    wall_datetime = _wall_datetime(wall)
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(blog, "_utcnow_iso", lambda: isoformat_utc(log()))
        )
        for module in (store_returned, store_records, bstore):
            stack.enter_context(patch.object(module, "datetime", wall_datetime))
        for module in (store_records, store_journal):
            stack.enter_context(patch.object(module, "utcnow", wall))
            stack.enter_context(patch.object(module, "generate_ulid", mint))
        if monotonic is not None or sleep is not None:
            fake = _Time(monotonic or _real_time.monotonic, sleep or (lambda _s: None))
            stack.enter_context(patch.object(bstore, "time", fake))
        yield
