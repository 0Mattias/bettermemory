"""The text, JSON and final status of the CLI's log verification report."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from bettermemory.cli.log_cmd import render_report
from bettermemory.history import history_replaced
from bettermemory.store import Store

from . import gen_store_files
from ._common import jsonl


def _case(index: int) -> dict[str, Any]:
    status = ("ok", "unverifiable", "tampered")[index % 3]
    head: dict[str, Any] = {"status": "matched"}
    if index % 4:
        head["seq"] = (0, 4, 2**74)[index % 3]
    label = (None, 1, 3.75, "é\n\t", ["leaf", 8], b"bin", [b"note", 5])[index % 7]
    missing = [label] if index % 5 else []
    counts = {"unaccounted": [], "missing": missing, "rows": 2}
    report: dict[str, Any] = {
        "status": status,
        "path": ("notes", "d/notes é", "dir/\udcff")[index % 3],
        "store_id": "sample",
        "key_fingerprint": "fp",
        "rows": 7,
        "segments": [
            {
                "first_seq": 2,
                "last_seq": 7,
                "status": "verified",
                "key_fingerprint": "α\x1b😀" * 7,
            }
        ],
        "head": head,
        "origin": {"status": "ok", "seq": 0, "reason": "sample\nreason"},
        "problems": [],
        "fold": {"status": "ok", "tables": {"memories": counts}},
        "unaccounted_ids": [],
    }
    if index % 4 == 0:
        del report["origin"]
    elif index % 4 == 1:
        report["origin"] = None
    if index % 3:
        report["fold"]["membership"] = {
            "projects": {"rows": 3, "missing": [], "unaccounted": [["item", None]]}
        }
    if index % 2:
        report["history_replaced"] = [
            {
                "seq": 6,
                "kind": "store_restored" if index % 3 else "history_accepted",
                "ts": "time\r",
                "from_seq": (3, 0, None)[index % 3],
                "to_seq": 3,
            }
        ]
    if index % 6:
        report["anchors"] = (
            [{"seq": 7, "status": status, "file": "x\u2028y", "reason": "why"}]
            if index % 2
            else []
        )
    if index % 3 == 2:
        report["problems"] = [{"seq": 2, "kind": "event", "problem": "detail\x1b"}]
    if index == 0:
        counts.update(unaccounted=["extra-one", "extra-two"], missing=["lost"])
        report["fold"]["membership"] = {
            name: {"rows": rows, "missing": [], "unaccounted": []}
            for name, rows in (("first", 2**132), ("second", -(2**130)), ("third", -8))
        }
        report["anchors"] = [
            {"seq": 8, "status": "ok", "file": "first"},
            {"seq": 2, "status": "mismatch", "reason": "digest", "file": "second"},
        ]
        report["history_replaced"] = [
            {"seq": 9, "ts": "early", "kind": "history_accepted", "to_seq": 4},
            {"seq": 10, "ts": "late", "kind": "store_restored", "to_seq": 7},
        ]
        report["problems"] = [
            {"seq": 5, "kind": "change", "problem": "first"},
            {"seq": 1, "kind": "older", "problem": "second"},
        ]
    text = render_report(report)
    code = 0 if status == "ok" else 1
    try:
        document = json.dumps(report, indent=2) + "\n"
        has_bytes = False
    except TypeError as exc:
        assert str(exc) == "Object of type bytes is not JSON serializable"
        has_bytes = True
        document = str(exc)
        # SQLite bytes are observable as Python str(label) in text mode.
        # The native Store projection is tested separately.
        counts["missing"] = [str(label)]
    return {
        "input": json.dumps(report, separators=(",", ":")),
        "bytes": has_bytes,
        "text": text,
        "json": document,
        "code": code,
    }


def _signed_cases() -> list[dict[str, Any]]:
    rows = []
    bases = gen_store_files.load_bases()
    with tempfile.TemporaryDirectory() as temp:
        for name in ("intact", "rekeyed", "restored", "accepted"):
            case = Path(temp) / name
            gen_store_files.rebuild(bases[name], case)
            with Store.open(
                case / "memory.sqlite", read_only=True, keys_dir=case / "keys"
            ) as store:
                report = store.log_verify()
                report["path"] = "notes/é.sqlite"
                history = history_replaced(store)
                if history:
                    report["history_replaced"] = history
            encoded = (json.dumps(report, indent=2) + "\n").encode("ascii")
            rows.append(
                {
                    "base": name,
                    "status": report["status"],
                    "text_sha256": hashlib.sha256(
                        render_report(report).encode()
                    ).hexdigest(),
                    "json_sha256": hashlib.sha256(encoded).hexdigest(),
                }
            )
    return rows


def generate() -> dict[str, bytes]:
    cases = [_case(i) for i in range(28)] + _signed_cases()
    return {"cli_log_report.jsonl": jsonl([{"cases": len(cases)}, *cases])}
