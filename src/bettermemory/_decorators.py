"""Cross-cutting decorators that capture recurring control-flow shapes.

Today: one decorator — ``best_effort`` — for the swallow-and-warn pattern
the FTS5 index helpers use. Pre-Round-2 this lived inline in
``store.py:_index_upsert_quietly`` / ``store.py:_index_remove_quietly``
(both literally try-call-except-log) and was a near-duplicate of the
shape ``hook.py`` uses for non-blocking I/O. Centralising lets the
log message format and the never-block-the-caller contract stay one
definition.

The decorator is intentionally narrow. Sites with idiosyncratic
recovery state (the per-model ``_LOAD_FAILED`` set in ``semantic.py``,
the report-failure rows in ``consolidate.py``) keep their bespoke
try/except — wrapping them in a generic decorator would HIDE the
recovery semantics the call site needs to be obvious about. The
audit's broader suggestion landed on these two store sites and
arguably ``semantic._maybe_hydrate_persistent_cache`` (handled
inline today; the cleanup state there reads naturally as inline
code, so it stays).
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable, TypeVar

T = TypeVar("T")


_DEFAULT_LOGGER = logging.getLogger("bettermemory")


def best_effort(
    operation: str,
    *,
    logger: logging.Logger | None = None,
    repair_hint: str | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T | None]]:
    """Wrap a function so it never raises — log a warning instead.

    Use for side-effects whose failure should not break the caller (e.g.
    "best-effort index upsert: a corrupt FTS5 database mustn't block the
    on-disk write that's the canonical record"). The wrapped function
    returns its normal value on success and ``None`` on failure; the
    warning is logged once per call with the exception class and message.

    ``operation`` is a short verb phrase that goes into the log line
    ("index upsert", "embedding cache flush"). ``repair_hint`` is an
    optional trailing fragment that tells the reader how to recover
    (typically "Run ``bettermemory reindex``"); when present it lands at
    the end of the warning, separated by a period.

    Catches the broad ``Exception`` rather than a specific type because
    every adopter site has the same "we don't know what the underlying
    library will raise, only that it must not abort the caller"
    requirement. Equivalent to ``# noqa: BLE001`` on the inline pattern.
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T | None]:
        log = logger if logger is not None else _DEFAULT_LOGGER

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T | None:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — that is the point.
                tail = f" {repair_hint}" if repair_hint else ""
                log.warning(
                    "%s failed: %s: %s.%s",
                    operation,
                    type(exc).__name__,
                    exc,
                    tail,
                )
                return None

        return wrapper

    return decorator


__all__ = ["best_effort"]
