"""Reference projections of the checked key state and its two pieces of history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bettermemory import log
from bettermemory import store_keys as keys
from tests.contract._common import jsonl

_ID = "a" * 32
_SYNTHETIC_KEY = bytes(range(32))
_HEAD = log.Head(5, "4" * 64, "fixture")
_END = log.LogRow(3, "now", None, "search", "{}", "3" * 64, "2" * 64)


def _json(value: Any) -> str:
    # Preserve insertion order in the reference's three nested dictionaries.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _fit(
    origin: tuple[str, int | None, str | None], status: str, *, head: bool, end: bool
) -> keys.RingFit:
    return keys.RingFit(
        log.OriginCheck(
            *origin, signer="d" * 64 if origin[0] == log.ORIGIN_OK else None
        ),
        _HEAD if head else None,
        status,
        _END if end else None,
    )


def generate() -> dict[str, bytes]:
    records: list[Any] = []
    origins = [
        (log.ORIGIN_ABSENT, None, None),
        (log.ORIGIN_OK, 1, None),
        (log.ORIGIN_UNVERIFIABLE, 1, None),
        (log.ORIGIN_MISMATCHED, 1, "missing"),
        (log.ORIGIN_MISMATCHED, 1, "mac"),
        (log.ORIGIN_MISMATCHED, 1, "signature"),
    ]
    statuses = [
        keys.HEAD_ABSENT,
        keys.HEAD_OK,
        keys.HEAD_STALE,
        keys.HEAD_PAST,
        keys.HEAD_MISMATCH,
    ]
    for index, origin in enumerate(origins):
        for j, status in enumerate(statuses):
            head = status != keys.HEAD_ABSENT
            end = (index + j) % 2 == 0
            fit = _fit(origin, status, head=head, end=end)
            records.append(
                {
                    "op": "fit",
                    "origin": origin,
                    "head_status": status,
                    "head": head,
                    "end": end,
                    "expected": _json(
                        [
                            fit.origin.to_dict(),
                            fit.origin.holds,
                            fit.origin.broken,
                            fit.carries_origin,
                            fit.reaches_head,
                            fit.fits,
                            fit.reason(),
                            fit.to_dict(),
                        ]
                    ),
                }
            )

    ring = log.KeyRing(Path("."), _ID)
    for kind in keys.KEY_STATES:
        for variant in range(3):
            held = _SYNTHETIC_KEY if variant != 0 else None
            expected = "1" * 14 + "🦩\udcffextra" if variant == 2 else "2" * 64
            error = (
                log.KeyUnreadableError(
                    ring.current_path(), OSError("failure"), reason="reason-\udcff"
                )
                if variant == 2
                else None
            )
            bound = (
                Path("other")
                if kind == keys.KEY_BOUND_ELSEWHERE or variant == 2
                else None
            )
            note = [None, "", "detail-\udcff"][variant]
            unfit = (
                _fit(origins[3], keys.HEAD_PAST, head=True, end=True)
                if variant == 2
                else None
            )
            state = keys.KeyState(
                kind,
                ring,
                Path("store"),
                expected,
                key=held,
                error=error,
                bound_to=bound,
                unread=note,
                unfit=unfit,
            )
            records.append(
                {
                    "op": "state",
                    "kind": kind,
                    "variant": variant,
                    "expected": _json(
                        [
                            str(state.key_path),
                            None if state.withheld is None else str(state.withheld),
                            state.held,
                            state.summary,
                            state.remedy,
                            state.refusal(Path("display-🦩")),
                            state.details(),
                        ]
                    ),
                }
            )
    return {"store_key_state.jsonl": jsonl(records)}


def signed() -> dict[str, bytes]:
    """Compare active and past writer checkpoints against two normally signed files."""
    import sqlite3
    import tempfile
    from contextlib import closing

    from bettermemory.store import Store
    from tests.contract._sandbox import world

    records: list[dict[str, Any]] = []
    with world(), tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        checkpoints: list[list[log.Head]] = []
        for label in ("first", "second"):
            area = root / label
            area.mkdir()
            heads: list[log.Head] = []
            with Store.create(
                area / "memory.sqlite", keys_dir=area / "keys", strict_provenance=False
            ) as store:
                for n in range(3):
                    if n:
                        store.record_event("search", session=label, query=f"sample {n}")
                    checkpoint = store.keyring.read_head()
                    assert checkpoint is not None
                    heads.append(checkpoint)
            checkpoints.append(heads)
        own, other = checkpoints
        uri = (root / "first" / "memory.sqlite").as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            cases = [
                ("absent", None),
                ("current", own[-1]),
                ("stale-first", own[0]),
                ("stale-middle", own[1]),
                ("other-current", other[-1]),
                ("other-middle", other[1]),
                ("before-first", log.Head(0, own[0].mac, "")),
                ("far-ahead", log.Head(2**104, own[-1].mac, "")),
                ("lower-bind", log.Head(-(2**63), own[0].mac, "")),
            ]
            for name, h in cases:
                try:
                    result = ["ok", keys.head_status(h, conn)]
                except OverflowError as err:
                    result = [type(err).__name__, str(err)]
                records.append({"case": name, "expected": result})
    return {"store_key_state_signed.jsonl": jsonl(records)}


def warning_process() -> dict[str, bytes]:
    """The store logger receives a path, summary and capitalized remedy as separate values."""
    import logging
    import os
    import tempfile
    from unittest.mock import patch

    rows: list[dict[str, Any]] = []
    with (
        tempfile.TemporaryDirectory() as tmp,
        patch.object(keys, "_BOUND_ELSEWHERE_WARNED", set()),
    ):
        root = Path(tmp).resolve()
        ring = log.KeyRing(root / "keys", _ID)
        target = root / "location.sqlite"
        other = root / "other.sqlite"
        target.touch()
        other.touch()

        def stable(raw: object) -> str:
            return str(raw).replace(str(root), "ROOT").replace(os.sep, "/")

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                assert isinstance(record.args, tuple)
                raw = [stable(arg) for arg in record.args]
                rows.append(
                    {
                        "level": record.levelname,
                        "logger": record.name,
                        "format": record.msg,
                        "arguments": raw,
                        "message": stable(record.getMessage()),
                    }
                )

        handler = Capture()
        old_level = keys.log.level
        keys.log.addHandler(handler)
        keys.log.setLevel(logging.DEBUG)
        try:
            keys._warn_bound_elsewhere(ring, target, ring.store_id, other, None)
            keys._warn_bound_elsewhere(
                ring, target, ring.store_id, other, "unreadable this time"
            )
            unclaimed = keys.KeyState(keys.KEY_UNCLAIMED, ring, target, "f" * 64)
            keys._warn_unclaimed(unclaimed)
            keys._warn_unclaimed(unclaimed)
        finally:
            keys.log.removeHandler(handler)
            keys.log.setLevel(old_level)
    return {"store_key_state_warnings.jsonl": jsonl(rows)}
