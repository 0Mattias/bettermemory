"""Fixtures: the process entry's hook-word routing and the parser-free
leaves of `cli._common` (``cli_common.jsonl``, ``cli_common_python.jsonl``).

The native build reads them in
``native/crates/bettermemory-core/src/entry.rs`` and
``native/crates/bettermemory-core/src/cli/common.rs``.

Each row names its function in ``f``:

- ``words``: `_daemon_client.HOOK_WORDS`, sorted;
- ``hook``: `_entry.route_hook_word(argv)` with `hook_main` replaced by one
  that records its argument and returns ``ret``: ``exit`` is the code it
  exited with (null where it returned) and ``called`` the argument;
- ``refuse``: `_refuse` over an `argparse.ArgumentParser(prog=prog)`:
  ``exit`` and ``err`` (its stderr);
- ``bundle``: `exit_if_bundle_file` with `backup.bundle_refusal` answering
  ``refusal``: ``exit`` (null where it returned) and ``err``;
- ``key``: `key_unreadable_line` and `exit_if_key_unreadable` over a
  `KeyUnreadableError` of ``path`` (a POSIX path, the same text on every
  system) and ``reason``: ``line``, ``exit`` and ``err``;
- ``migration``: `migration_line` over the totals ``t``;
- ``cutoff``: `parse_iso_cutoff(value, flag=, far_future=)` with
  `datetime.now` answering ``now``: ``ok`` (the parsed value's isoformat
  and the canonical text), ``exit``, ``raised`` (the exception's class)
  and ``err`` (stderr, also where it returned).

A string holding a lone surrogate is written as code points (``*_cps``).
``cli_common_python.jsonl`` holds the cutoffs whose answer is
`datetime.fromisoformat`'s edges, which follow the Python release.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from unittest import mock

from bettermemory import _daemon_client, _entry, backup
from bettermemory.cli import _common
from bettermemory.log import KeyUnreadableError

from ._common import jsonl, text_field


def _run(call: Any) -> tuple[Any, int | None, str, str | None]:
    err = io.StringIO()
    result = None
    code: int | None = None
    raised: str | None = None
    with contextlib.redirect_stderr(err):
        try:
            result = call()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else -1
        except (OverflowError, ValueError) as exc:
            raised = type(exc).__name__
    return result, code, err.getvalue(), raised


def _hook(argv: list[str], ret: int = 0) -> dict[str, Any]:
    seen: list[list[str]] = []

    def hook_main(a: list[str]) -> int:
        seen.append(list(a))
        return ret

    with mock.patch.object(_daemon_client, "hook_main", hook_main):
        _, code, _, _ = _run(lambda: _entry.route_hook_word(argv))
    return {
        "f": "hook",
        "argv": argv,
        "ret": ret,
        "exit": code,
        "called": seen[0] if seen else None,
    }


# Python 3.14's argparse colours "usage:" and "error:" under FORCE_COLOR or
# PYTHON_COLORS even into a StringIO; the rows pin the plain text.
_COLOUR_CLEARED = ("FORCE_COLOR", "PYTHON_COLORS", "CLICOLOR", "CLICOLOR_FORCE")


def _refuse(prog: str, message: str, code: int) -> dict[str, Any]:
    parser = argparse.ArgumentParser(prog=prog)
    with mock.patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "dumb"}):
        for name in _COLOUR_CLEARED:
            os.environ.pop(name, None)
        _, got, err, _ = _run(lambda: _common._refuse(parser, message, code))
    return {
        "f": "refuse",
        "prog": prog,
        "message": message,
        "code": code,
        "exit": got,
        "err": err,
    }


def _bundle(refusal: str | None, code: int, prefix: str) -> dict[str, Any]:
    with mock.patch.object(backup, "bundle_refusal", lambda _p: refusal):
        _, got, err, _ = _run(
            lambda: _common.exit_if_bundle_file(
                Path("store.db"),
                exit_code=code,
                prefix=prefix,
            )
        )
    return {
        "f": "bundle",
        "refusal": refusal,
        "code": code,
        "prefix": prefix,
        "exit": got,
        "err": err,
    }


def _key(path: str, reason: str, prefix: str, code: int = 1) -> dict[str, Any]:
    exc = KeyUnreadableError(
        PurePosixPath(path),  # type: ignore[arg-type]
        PermissionError(13, "Permission denied"),
        reason=reason,
    )
    line = _common.key_unreadable_line(exc, prefix=prefix)

    def block() -> None:
        with _common.exit_if_key_unreadable(exit_code=code, prefix=prefix):
            raise exc

    _, got, err, _ = _run(block)
    return {
        "f": "key",
        **text_field("path", path),
        "reason": reason,
        "prefix": prefix,
        "code": code,
        **text_field("line", line),
        "exit": got,
        **text_field("err", err),
    }


class _Totals:
    def __init__(self, **kw: int) -> None:
        for name in (
            "projects",
            "checkouts",
            "memory_homes",
            "tombstone_homes",
            "episode_homes",
            "proposals",
            "log_rows",
        ):
            setattr(self, name, kw.get(name, 0))


class _Report:
    def __init__(self, totals: _Totals) -> None:
        self.totals = totals
        self.homed = totals.memory_homes + totals.tombstone_homes + totals.episode_homes  # type: ignore[attr-defined]


def _migration(**kw: int) -> dict[str, Any]:
    line = _common.migration_line(_Report(_Totals(**kw)))  # type: ignore[arg-type]
    return {"f": "migration", "t": kw, "line": line}


NOW = "2026-05-25T12:00:00.250000+00:00"


def _cutoff(
    value: str, *, far: str = "refuse", now: str = NOW, flag: str = "--since"
) -> dict[str, Any]:
    fixed = datetime.fromisoformat(now)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            return fixed

    with mock.patch.object(_common, "datetime", _Clock):
        result, code, err, raised = _run(
            lambda: _common.parse_iso_cutoff(value, flag=flag, far_future=far)
        )
    row: dict[str, Any] = {
        "f": "cutoff",
        **text_field("value", value),
        "flag": flag,
        "far": far,
        "now": now,
    }
    if result is not None:
        row["ok"] = [result[0].isoformat(), result[1]]
    row["exit"] = code
    row["raised"] = raised
    row.update(text_field("err", err))
    return row


def _stable() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {"f": "words", "v": sorted(_daemon_client.HOOK_WORDS)}
    ]
    for argv, ret in (
        ([], 0),
        (["hook"], 0),
        (["hook", "stop"], 3),
        (["session-start"], 0),
        (["audit-turn", "x"], 1),
        (["prompt-recall"], 2),
        (["Hook"], 0),
        (["hook "], 0),
        (["serve", "hook"], 0),
        ([""], 0),
        (["stop"], 0),
    ):
        rows.append(_hook(argv, ret))
    for prog, message, code in (
        ("bm", "no store", 2),
        ("bm", "no store", 1),
        ("bettermemory show", "bad\nline", 3),
        ("bm", "", 0),
        ("x", "é  ", 75),
    ):
        rows.append(_refuse(prog, message, code))
    for refusal, code, prefix in (
        (None, 1, "bettermemory"),
        ("a bundle file", 1, "bettermemory"),
        ("r", 2, "bettermemory serve"),
        ("multi\nline", 0, ""),
    ):
        rows.append(_bundle(refusal, code, prefix))
    for path, reason, prefix, code in (
        ("/k/current.key", "Permission denied", "bettermemory", 1),
        ("/k/current.key", "a link to /x\nnext", "bm", 3),
        ("/k/a\tb", "esc \x1b del \x7f nel \x85 ls   é", "p\n", 1),
        ("/k/\udcff\udc80", "Input/output error", "bettermemory", 1),
        ("relative/key", "", "", 0),
    ):
        rows.append(_key(path, reason, prefix, code))
    rows.append(_migration())
    rows.append(
        _migration(
            projects=1,
            checkouts=2,
            memory_homes=3,
            tombstone_homes=4,
            episode_homes=5,
            proposals=6,
            log_rows=7,
            views=8,
        )
    )
    rows.append(
        _migration(
            memory_homes=2**62, tombstone_homes=2**62, episode_homes=2**62, log_rows=-1
        )
    )
    for value, far in (
        ("2026-05-25T05:25:35Z", "refuse"),
        ("2026-05-25T05:25:35+02:00", "refuse"),
        ("2026-05-25T05:25:35.123456-04:30", "warn"),
        ("2026-05-25T10:00:00", "refuse"),
        ("2026-05-25", "refuse"),
        ("2026-13-01", "refuse"),
        ("nope", "refuse"),
        ("", "refuse"),
        ("2026-05-25T05:25:35ZZ", "refuse"),
        ("it's", "refuse"),
        ("2126-05-25T00:00:00Z", "refuse"),
        ("2126-05-25T00:00:00Z", "warn"),
        ("2126-05-25T00:00:00Z", "other"),
        ("2026-05-26T12:00:00.250000Z", "refuse"),
        ("2026-05-26T12:00:00.250001Z", "refuse"),
        ("2026-05-26T12:00:00.250001Z", "warn"),
        ("2026-05-26T14:00:00.250001+02:00", "refuse"),
        ("1970-01-01T00:00:00Z", "refuse"),
    ):
        rows.append(_cutoff(value, far=far))
    rows.append(_cutoff("2026-05-25T05:25:35Z", flag="--acknowledge-misses-before"))
    rows.append(_cutoff("2026-05-25T05:25:35Z", now="2026-05-25T12:00:00+00:00"))
    return rows


def _python() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for value, far, now in (
        ("2026-05-25T05:25:35.1Z", "refuse", NOW),
        ("2026-05-25T05:25:35,5Z", "refuse", NOW),
        ("20260525T052535Z", "refuse", NOW),
        ("2026-05-25 05:25Z", "refuse", NOW),
        ("2026-05-25T05Z", "refuse", NOW),
        ("2026-W21-1T00:00Z", "refuse", NOW),
        ("2026-145T00:00Z", "refuse", NOW),
        ("2026-05-25T05:25:35+05:30:15.5", "refuse", NOW),
        ("2026-05-25T05:25:35+0530", "refuse", NOW),
        ("2026-05-25T05:25:35-23:59:59.999999", "refuse", NOW),
        ("2026-05-25T05:25:35+24:00", "refuse", NOW),
        ("2026-05-25T24:00:00Z", "refuse", NOW),
        ("2026-02-29T00:00:00Z", "refuse", NOW),
        ("2026-05-25T05:25:35z", "refuse", NOW),
        ("2026-05-25t05:25:35Z", "refuse", NOW),
        (" 2026-05-25T05:25:35Z", "refuse", NOW),
        ("2026-05-25T05:25:35Z ", "refuse", NOW),
        ("2026-05-25TZ", "refuse", NOW),
        ("Z", "refuse", NOW),
        ("٢٠٢٦-٠٥-٢٥", "refuse", NOW),
        ("２０２６-０５-２５", "refuse", NOW),
        ("2026-05-25\udcff", "refuse", NOW),
        ("\udcff\udcff\udcff\udcff-\udcff\udcff-\udcff\udcff", "refuse", NOW),
        ("2026-05-25T05:25:35é", "refuse", NOW),
        ("20260525", "refuse", NOW),
        ("2026-0525", "refuse", NOW),
        ("0001-01-01T00:00:00+01:00", "refuse", NOW),
        ("0001-01-01T00:59:59.999999+01:00", "warn", NOW),
        ("9999-12-31T23:59:59-01:00", "warn", NOW),
        ("9999-12-31T23:59:59-01:00", "refuse", NOW),
        ("9999-12-31T23:59:59Z", "refuse", NOW),
        ("2026-05-25T05:25:35Z", "refuse", "9999-12-31T00:00:00+00:00"),
        ("9999-12-31T00:00:00Z", "refuse", "9999-12-30T23:59:59.999999+00:00"),
        ("2026-05-25T05:25:35\x00Z", "refuse", NOW),
        ("2026-05-25T05:25:35.1234567Z", "refuse", NOW),
        ("2026-05-25T05:25:35.12Z", "refuse", NOW),
        ("2026-05-25T05:25:35+00:00Z", "refuse", NOW),
        ("2026-05-25T05:25:35-00:00", "refuse", NOW),
    ):
        rows.append(_cutoff(value, far=far, now=now))
    return rows


def _file(rows: list[dict[str, Any]]) -> bytes:
    return jsonl([{"cases": len(rows)}, *rows])


def generate() -> dict[str, bytes]:
    return {"cli_common.jsonl": _file(_stable())}


def generate_python() -> dict[str, bytes]:
    return {"cli_common_python.jsonl": _file(_python())}
