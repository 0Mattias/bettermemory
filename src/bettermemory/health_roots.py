"""Process-local progress for bounded estate reads (ENG-238).

The cursor is independent of the bounded timestamp history, so evicting an
old timestamp cannot put its root back at the front and starve later roots.
Each open Store owns its progress for its lifetime. Nothing is written to
the store or the filesystem; offline CLI audits visit every root instead.
This is scheduling state, not an exact cache registered with ``_caches``.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
from typing import Any
from weakref import WeakKeyDictionary

# A metadata bound, not a coverage bound. Beyond it a previous attempt's
# time becomes unknown; the cursor still advances through every root.
_LAST_CHECKED_CAP = 1024


@dataclass
class EstateProgress:
    """A rotating root cursor and the most recent known attempt times."""

    cursor: str | None = None
    checked: OrderedDict[str, datetime] = field(default_factory=OrderedDict)
    lock: RLock = field(default_factory=RLock, repr=False)

    def ordered(self, roots: Iterable[str]) -> list[str]:
        """Stable lexical order, starting after the last attempted root."""
        ordered = sorted(set(roots))
        start = bisect_right(ordered, self.cursor) if self.cursor is not None else 0
        return ordered[start:] + ordered[:start]

    def attempt(self, root: str, when: datetime) -> None:
        """Advance even after an unread attempt, without claiming it succeeded."""
        self.cursor = root
        self.checked[root] = when
        self.checked.move_to_end(root)
        while len(self.checked) > _LAST_CHECKED_CAP:
            self.checked.popitem(last=False)


_PROGRESS: WeakKeyDictionary[Any, EstateProgress] = WeakKeyDictionary()
_LOCK = RLock()


def progress_for_store(store: Any) -> EstateProgress:
    """Share progress across reports on one open Store, without retaining it.

    A duck-typed store that cannot be a weak key gets a report-local state;
    such callers can pass an explicit progress object to ``compute_health``.
    """
    with _LOCK:
        try:
            progress = _PROGRESS.get(store)
            if progress is None:
                progress = EstateProgress()
                _PROGRESS[store] = progress
            return progress
        except TypeError:
            return EstateProgress()
