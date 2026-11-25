"""A reader's open of a signed store: which key it keeps for checking.

Each case builds a store and a key ring from scratch, opens the store with
``Store.open(path, keys_dir=..., read_only=True)`` and records the key
state the open read, ``has_key``, ``can_verify``, whether a read key is
kept (`store_open._open_reader`) and whether it is the ring's current key,
what the reader says withholds the ring (``_bound_elsewhere``), and
whether the reader's step read the origin anchor or the log's segments;
or the class of the exception the open raised. Keys are fresh random
bytes, so a row records only how the files relate, never a key, a path
or a time.

``store_open_reader_python`` holds the cases whose answer is where
`json.loads` stops recursing (an origin anchor and a rekey payload nested
100,000 deep, and an origin anchor over 1 MiB nested 600,000 deep):
3.11 to 3.13, and 3.14 on macOS, raise RecursionError; 3.14 on Linux
reads them. The cases that never read the deep anchor stay
in the ungated file: no release reads it there.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from bettermemory import log, store_open
from bettermemory import store as _store
from bettermemory.store import Store

from ._common import jsonl
from ._sandbox import world

#: Nested past the JSON depth limit.
DEEP = 100_000
#: Nested past the JSON depth limit, in a file over 1 MiB.
BIG_DEEP = 600_000
#: Spaces after an anchor that parses, making its file over 1 MiB.
BIG_PAD = 1 << 20
#: Every row this generator appends carries this time.
TS = "2026-01-01T00:00:00+00:00"


class Case:
    """The files of one case: the store, its keys directory and the
    paths the reader may name as withholding the ring."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "memory.sqlite"
        self.keys = root / "keys"
        self.original = self.path
        self.store_id = ""

    @property
    def ring(self) -> log.KeyRing:
        return log.KeyRing(self.keys, self.store_id)


def _set_meta(path: Path, fingerprint: str) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'key_fingerprint'", (fingerprint,)
        )


def _append(path: Path, key: bytes, kind: str, payload: str) -> None:
    """Append one row signed by ``key`` and chained to the last, its
    payload text as given."""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        last = log.last_row(conn)
        assert last is not None
        seq = last.seq + 1
        mac = log.compute_mac(
            key,
            seq=seq,
            ts=TS,
            session=None,
            kind=kind,
            payload=payload,
            prev_mac=last.mac,
        )
        conn.execute(
            "INSERT INTO log(seq, ts, session, kind, payload, prev_mac, mac) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?)",
            (seq, TS, kind, payload, last.mac, mac),
        )
        conn.commit()


def _rekey(path: Path, signer: bytes, old: bytes, new: bytes) -> None:
    _append(
        path,
        signer,
        log.REKEY,
        log.canonical_payload(
            {
                "old_fingerprint": log.fingerprint(old),
                "new_fingerprint": log.fingerprint(new),
            }
        ),
    )


def _key() -> bytes:
    return os.urandom(log.KEY_BYTES)


def _own(case: Case) -> None:
    """A store this machine created, with one event after its first row."""
    with Store.create(case.path, keys_dir=case.keys, strict_provenance=False) as owner:
        owner.record_event("search", session="ordinary", query="sample")
        case.store_id = owner.store_id


def _theirs(case: Case) -> tuple[bytes, bytes]:
    """A store another machine created under its key; this machine's ring
    holds only a key of its own, bound to no file. Returns (theirs, ours)."""
    with Store.create(
        case.path, keys_dir=case.root / "theirs", strict_provenance=False
    ) as owner:
        case.store_id = owner.store_id
    theirs = log.KeyRing(case.root / "theirs", case.store_id).load_current()
    assert theirs is not None
    ours = _key()
    case.keys.mkdir()
    case.ring.current_path().write_bytes(ours)
    return theirs, ours


def _copy(case: Case) -> None:
    """Open a copy of the store: the ring stays bound to the original."""
    copy = case.root / "copy" / "memory.sqlite"
    copy.parent.mkdir()
    with (
        closing(sqlite3.connect(case.path)) as source,
        closing(sqlite3.connect(copy)) as dest,
    ):
        source.backup(dest)
    case.path = copy


def _drop_tail(case: Case) -> None:
    """The copy ends before the ring's head: an older copy of the store."""
    with closing(sqlite3.connect(case.path)) as conn, conn:
        conn.execute("DELETE FROM log WHERE seq = (SELECT MAX(seq) FROM log)")


def _deep_text() -> str:
    return "[" * DEEP + "]" * DEEP


def _big_anchor() -> str:
    """An anchor that parses (a seq and a MAC), in a file over 1 MiB."""
    return '{"seq": 1, "mac": "' + "0" * 64 + '"}' + " " * BIG_PAD


def _big_deep_text() -> str:
    return "[" * BIG_DEEP + "]" * BIG_DEEP


def _origin(case: Case, text: str | None) -> None:
    if text is None:
        case.ring.origin_path().unlink()
    else:
        case.ring.origin_path().write_text(text, encoding="utf-8")


def _edit_meta(case: Case) -> None:
    _set_meta(case.path, log.fingerprint(_key()))


def _bad_first_row(case: Case) -> None:
    """The first row's ``ts`` is text that is not UTF-8, which sqlite3
    cannot decode."""
    with closing(sqlite3.connect(case.path)) as conn, conn:
        conn.execute(
            "UPDATE log SET ts = CAST(X'FF' AS TEXT) WHERE seq = "
            "(SELECT MIN(seq) FROM log WHERE seq > 0)"
        )


def _new_ring_key(case: Case) -> None:
    case.ring.current_path().write_bytes(_key())


def _scenarios() -> list[tuple[str, bool, Callable[[Case], None]]]:
    """(name, deep: whether its answer follows where json.loads stops
    recursing, how its files are made)."""

    def present(case: Case) -> None:
        _own(case)

    def present_deep_anchor(case: Case) -> None:
        _own(case)
        _origin(case, _deep_text())

    def missing(case: Case) -> None:
        _own(case)
        case.ring.current_path().unlink()

    def missing_deep_anchor(case: Case) -> None:
        missing(case)
        _origin(case, _deep_text())

    def unreadable(case: Case) -> None:
        _own(case)
        case.ring.current_path().unlink()
        case.ring.current_path().mkdir()

    def foreign_anchored(case: Case) -> None:
        _own(case)
        _edit_meta(case)

    def foreign_anchored_bad_log(case: Case) -> None:
        foreign_anchored(case)
        _bad_first_row(case)

    def foreign_uppercase_meta(case: Case) -> None:
        # Meta names this machine's key in capitals: not a match, which
        # compares the text exactly, so the anchor is read.
        _own(case)
        ours = case.ring.load_current()
        assert ours is not None
        _set_meta(case.path, log.fingerprint(ours).upper())

    def foreign_edited(case: Case) -> None:
        # An editor pointed meta at another key; no anchor, so the log
        # decides, and its first row verifies under the key in hand.
        _own(case)
        _edit_meta(case)
        _origin(case, None)

    def foreign_leading(case: Case) -> None:
        # The first rekey row names this machine's key as the old one: the
        # leading segment is under it by name, and its first row verifies.
        _own(case)
        ours = case.ring.load_current()
        assert ours is not None
        other = _key()
        _rekey(case.path, other, ours, other)
        _set_meta(case.path, log.fingerprint(other))
        _origin(case, None)

    def foreign_later(case: Case) -> None:
        theirs, ours = _theirs(case)
        last = _key()
        _rekey(case.path, ours, theirs, ours)
        _rekey(case.path, last, ours, last)
        _set_meta(case.path, log.fingerprint(last))

    def foreign_named_unsigned(case: Case) -> None:
        # A rekey row names this machine's key, signed by another.
        theirs, ours = _theirs(case)
        _rekey(case.path, theirs, theirs, ours)

    def foreign_signs_nothing(case: Case) -> None:
        _theirs(case)

    def foreign_retired_meta(case: Case) -> None:
        # Meta names a retired key the ring holds; the first row is signed
        # by the ring's current key, the key in hand. The leading segment
        # is named by meta, not by the key in hand: nothing is kept.
        _own(case)
        retired = _key()
        case.ring.retired_path(log.fingerprint(retired)).write_bytes(retired)
        _set_meta(case.path, log.fingerprint(retired))
        _origin(case, None)

    def foreign_big_anchor(case: Case) -> None:
        # The ring is bound to this file and holds an anchor that parses,
        # in a file over 1 MiB: it keeps the key, which signs nothing.
        _theirs(case)
        case.ring.rebind_store(case.path)
        _origin(case, _big_anchor())

    def foreign_big_deep_anchor(case: Case) -> None:
        _theirs(case)
        case.ring.rebind_store(case.path)
        _origin(case, _big_deep_text())

    def bound_elsewhere_match(case: Case) -> None:
        _own(case)
        _copy(case)

    def bound_elsewhere_missing(case: Case) -> None:
        bound_elsewhere_match(case)
        case.ring.current_path().unlink()

    def bound_elsewhere_match_deep_anchor(case: Case) -> None:
        bound_elsewhere_match(case)
        _origin(case, _deep_text())

    def bound_elsewhere_foreign_anchored(case: Case) -> None:
        bound_elsewhere_match(case)
        _edit_meta(case)

    def bound_elsewhere_foreign(case: Case) -> None:
        bound_elsewhere_match(case)
        _new_ring_key(case)
        _origin(case, None)

    def unclaimed_match(case: Case) -> None:
        _own(case)
        _copy(case)
        _drop_tail(case)
        case.ring.binding_path().unlink()

    def unclaimed_foreign(case: Case) -> None:
        unclaimed_match(case)
        _new_ring_key(case)
        _origin(case, None)

    def anchor_not_json(case: Case) -> None:
        foreign_edited(case)
        _origin(case, "not json")

    def anchor_not_an_anchor(case: Case) -> None:
        foreign_edited(case)
        _origin(case, "{}")

    def anchor_not_json_signs_nothing(case: Case) -> None:
        _theirs(case)
        _origin(case, "not json")

    def first_row_unreadable(case: Case) -> None:
        foreign_edited(case)
        _bad_first_row(case)

    def anchor_deep_foreign(case: Case) -> None:
        foreign_edited(case)
        _origin(case, _deep_text())

    def rekey_deep_payload(case: Case) -> None:
        theirs, _ = _theirs(case)
        _append(case.path, theirs, log.REKEY, _deep_text())

    return [
        ("present", False, present),
        ("present-deep-anchor", False, present_deep_anchor),
        ("missing", False, missing),
        ("missing-deep-anchor", False, missing_deep_anchor),
        ("unreadable", False, unreadable),
        ("foreign-anchored", False, foreign_anchored),
        ("foreign-anchored-bad-log", False, foreign_anchored_bad_log),
        ("foreign-uppercase-meta", False, foreign_uppercase_meta),
        ("foreign-edited", False, foreign_edited),
        ("foreign-leading", False, foreign_leading),
        ("foreign-later", False, foreign_later),
        ("foreign-named-unsigned", False, foreign_named_unsigned),
        ("foreign-signs-nothing", False, foreign_signs_nothing),
        ("foreign-retired-meta", False, foreign_retired_meta),
        ("foreign-big-anchor", False, foreign_big_anchor),
        ("bound-elsewhere-match", False, bound_elsewhere_match),
        (
            "bound-elsewhere-match-deep-anchor",
            False,
            bound_elsewhere_match_deep_anchor,
        ),
        ("bound-elsewhere-foreign-anchored", False, bound_elsewhere_foreign_anchored),
        ("bound-elsewhere-foreign", False, bound_elsewhere_foreign),
        ("bound-elsewhere-missing", False, bound_elsewhere_missing),
        ("unclaimed-match", False, unclaimed_match),
        ("unclaimed-foreign", False, unclaimed_foreign),
        ("anchor-not-json", False, anchor_not_json),
        ("anchor-not-an-anchor", False, anchor_not_an_anchor),
        ("anchor-not-json-signs-nothing", False, anchor_not_json_signs_nothing),
        ("first-row-unreadable", False, first_row_unreadable),
        ("anchor-deep-foreign", True, anchor_deep_foreign),
        ("rekey-deep-payload", True, rekey_deep_payload),
        ("foreign-big-deep-anchor", True, foreign_big_deep_anchor),
    ]


@contextmanager
def _counted() -> Iterator[dict[str, int]]:
    """Count the origin anchor's and the segments' reads made inside
    `store_open._open_reader`, the reader's step."""
    counts = {"origin": 0, "segments": 0, "inside": 0}
    reader = store_open._open_reader
    read_origin = log.KeyRing.read_origin
    chain_segments = _store.chain_segments

    def open_reader(*args: Any, **kwargs: Any) -> Store:
        counts["inside"] += 1
        try:
            return reader(*args, **kwargs)
        finally:
            counts["inside"] -= 1

    def origin(self: log.KeyRing) -> log.HistoryOrigin | None:
        if counts["inside"]:
            counts["origin"] += 1
        return read_origin(self)

    def segments(*args: Any, **kwargs: Any) -> list[log.Segment]:
        if counts["inside"]:
            counts["segments"] += 1
        return chain_segments(*args, **kwargs)

    with (
        patch.object(store_open, "_open_reader", open_reader),
        patch.object(log.KeyRing, "read_origin", origin),
        patch.object(_store, "chain_segments", segments),
    ):
        yield counts


def _withheld(case: Case, elsewhere: Path | None) -> str | None:
    if elsewhere is None:
        return None
    if elsewhere == case.original:
        return "original"
    if elsewhere == case.ring.binding_path():
        return "binding"
    return "other"


def _open(case: Case) -> dict[str, Any]:
    with _counted() as counts:
        try:
            opened = Store.open(case.path, keys_dir=case.keys, read_only=True)
        except Exception as exc:  # noqa: BLE001 - the class is the answer
            return {"raised": type(exc).__name__}
    with opened:
        try:
            current = case.ring.load_current()
        except log.KeyUnreadableError:
            current = None
        read_key = opened._read_key
        return {
            "state": opened.key_state.state,
            "has_key": opened.has_key,
            "can_verify": opened.can_verify,
            "read_key": read_key is not None,
            "read_key_current": read_key is not None and read_key == current,
            "bound_elsewhere": _withheld(case, opened._bound_elsewhere),
            "origin_read": counts["origin"] > 0,
            "log_read": counts["segments"] > 0,
        }


def _rows(deep: bool) -> list[dict[str, Any]]:
    rows = []
    with world(), TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        for name, is_deep, build in _scenarios():
            if is_deep != deep:
                continue
            root = base / name
            root.mkdir()
            case = Case(root)
            build(case)
            rows.append({"case": name, "result": _open(case)})
            shutil.rmtree(root, ignore_errors=True)
    return rows


def generate() -> dict[str, bytes]:
    return {"store_open_reader.jsonl": jsonl(_rows(deep=False))}


def generate_python() -> dict[str, bytes]:
    """The cases whose answer is where `json.loads` stops recursing."""
    return {"store_open_reader_python.jsonl": jsonl(_rows(deep=True))}
