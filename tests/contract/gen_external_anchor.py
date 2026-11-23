"""External anchors read from real files and the historical signed intact store."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bettermemory import anchor
from bettermemory.store import Store

from ._common import jsonl
from .gen_store_files import GOLDEN, rebuild

STAMP = datetime(2021, 2, 3, tzinfo=timezone.utc)
BASE = {
    "format": anchor.ANCHOR_FORMAT,
    "store_id": "store",
    "seq": 1,
    "mac": "a" * 64,
    "chain_sha256": "b" * 64,
    "created_at": "recorded",
}


def _wire(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _outcome(call: Callable[[], Any]) -> str:
    try:
        return _wire(["ok", call()])
    except (ValueError, OverflowError, RecursionError, KeyError, TypeError) as error:
        return _wire(["error", type(error).__name__, str(error)])


def generate() -> dict[str, bytes]:
    rows: list[Any] = []
    variants: list[Any] = [BASE, None, [], True, 42, "document", {}]
    for key, choices in (
        ("format", ["v2", None, "🐾", {"odd": [False, 3]}]),
        ("store_id", ["", "\udcfd", "\n", 4]),
        ("seq", [True, False, 0, -1, 1.5, "1", 2**63 - 1, 2**63, 2**120]),
        ("mac", ["f" * 64, "F" * 64, "0" * 63, "0" * 64 + "\n", None]),
        ("chain_sha256", ["0" * 64, "g" * 64, "é" * 64, 1]),
        ("created_at", ["", None, "free\ud800", "\u2003"]),
    ):
        variants.extend({**BASE, key: v} for v in choices)
        variants.append({k: v for k, v in BASE.items() if k != key})
    variants.append({**BASE, "extra": [3, None, {}]})
    for i, value in enumerate(variants):
        place = f"source {i}\udcff"
        rows.append(
            {
                "op": "parse",
                "raw": _wire(value),
                "place": _wire(place),
                "expected": _outcome(
                    lambda: anchor.parse_anchor(value, where=place).to_dict()
                ),
            }
        )
    one = _wire(BASE).encode()
    raws = [
        one,
        b"\xef\xbb\xbf" + one,
        b"\xef\xbb\xbf\xef\xbb\xbf" + one,
        b" ",
        b"",
        b"false",
        b"[]",
        b"0",
        b"not json",
        b"\xff",
        b"\n\xc3",
        b"\xe2\x82",
        b"\r\n" + one + b"\n\r" + one + b"\n",
        one + "\u2028\u2003\u0085".encode() + one,
        one + b"\n\n\xff",
        one + b"\n\r{}\n",
        one + b"\n   {",
        one + b"\n" + b"1" * 4301,
        one + "\u2029\u2028".encode() + b"true",
        json.dumps(BASE, indent=2).encode(),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for raw in raws:
            path = Path(tmp) / "anchor"
            path.write_bytes(raw)
            expected = _outcome(
                lambda: [a.to_dict() for a in anchor.load_anchors(path)]
            )
            expected = _wire(
                [
                    part.replace(str(path), "FILE") if isinstance(part, str) else part
                    for part in json.loads(expected)
                ]
            )
            rows.append({"op": "decode", "raw": raw.hex(), "expected": expected})
    return {"external_anchor.jsonl": jsonl(rows)}


def posix() -> dict[str, bytes]:
    if not hasattr(os, "mkfifo"):
        return {}
    rows: list[Any] = []
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "regular").write_text(_wire(BASE), encoding="ascii")
        (folder / "linked").symlink_to("regular")
        os.mkfifo(folder / "pipe")
        (folder / "pipe-link").symlink_to("pipe")
        for name in ("regular", "linked", "pipe", "pipe-link", "absent", "raw-\udcff"):
            expected = _outcome(
                lambda: [a.to_dict() for a in anchor.load_anchors(folder / name)]
            )
            decoded = json.loads(expected)
            row = {"name": _wire(name)}
            if decoded[0] == "error":
                message = decoded[2].replace(str(folder) + "/", "")
                # The OS quotes an undecodable path in its message. Preserve both
                # its surrogate and the literal backslash in its repr.
                rows.append(
                    {**row, "error": decoded[1], "message": list(map(ord, message))}
                )
            else:
                rows.append({**row, "expected": _wire(decoded)})
    return {"external_anchor_files_posix.jsonl": jsonl(rows)}


def signed() -> dict[str, bytes]:
    rows: list[Any] = []
    base = json.loads((GOLDEN / "store_bases.json").read_text())["bases"]["intact"]
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "memory"
        rebuild(base, folder)
        with Store.open(folder, keys_dir=folder / "keys", read_only=True) as store:
            tip = anchor.take(store, now=STAMP)
            mac = store.conn.execute("SELECT mac FROM log WHERE seq = 1").fetchone()[0]
            first = replace(
                tip,
                seq=1,
                mac=mac,
                chain_sha256=anchor.chain_digests(store.conn, [1])[1],
            )
            checks = [
                ("tip", tip),
                ("older", first),
                ("duplicate", first),
                ("foreign", replace(tip, store_id="another store", seq=2**100)),
                ("beyond", replace(tip, seq=tip.seq + 9)),
                ("bad\udcff", replace(tip, mac="0" * 64)),
                ("other-digest", replace(first, chain_sha256="1" * 64)),
            ]
            for group in ([], checks[:1], checks[3:4], checks, checks[::-1]):
                result = anchor.check_anchors(store.conn, store.store_id, group)
                report = {
                    "status": "ok",
                    "existing": None,
                    "problems": [
                        {
                            "seq": 1,
                            "problem": "anchor_mismatch",
                            "detail": "already seen",
                        }
                    ],
                }
                anchor.merge_into_report(report, result)
                rows.append(
                    {
                        "store_id": store.store_id,
                        "anchors": [
                            [_wire(name), _wire(a.to_dict())] for name, a in group
                        ],
                        "results": _wire(result),
                        "report": _wire(report),
                    }
                )
    return {"external_anchor_signed.jsonl": jsonl(rows)}
