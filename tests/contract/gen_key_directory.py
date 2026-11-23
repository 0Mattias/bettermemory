"""The POSIX directory selected for a Store's signing keys, before filesystem encoding."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from bettermemory import log, store_open, store_schema
from bettermemory.store import Store

from ._common import jsonl


def _points(value: str | None) -> list[int] | None:
    return None if value is None else list(map(ord, value))


def _selected(directory: Path) -> dict[str, Any]:
    text = str(directory)
    result: dict[str, Any] = {"path": _points(text)}
    try:
        result["bytes"] = list(os.fsencode(text))
    except UnicodeEncodeError as exc:
        assert exc.object == text
        result["encoding_error"] = [
            exc.encoding,
            exc.start,
            exc.end,
            exc.reason,
            str(exc),
        ]
    return result


def _from_store() -> bytes:
    rows: list[dict[str, Any]] = []

    class ReachedRing(Exception):
        pass

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        db = root / "memory.sqlite"
        Store.create(db, keys_dir=root / "keys").close()

        # Capture the real KeyRing after the ordinary read-only opener has
        # constructed its exact directory, before any key path is encoded.
        def reached(directory: Path, store_id: str) -> None:
            ring = log.KeyRing(directory, store_id)
            raise ReachedRing(ring.keys_dir)

        explicit = (
            "",
            ".",
            "././",
            "..",
            "a//b/./c/",
            "~",
            "~/keys",
            "~~/text",
            "//",
            "///",
            "//double/../dir/",
            "Ω/🪴",
            "a\0b",
            "é",
            "\udcc3\udca9",
            "x\udcff",
            "e\ud800",
            "a\udc80\ud800\udc80",
            "q\ud800\ud801",
        )
        with (
            patch.dict(os.environ, {"BETTERMEMORY_KEYS_DIR": "~unused-account/keys"}),
            patch.object(store_open, "KeyRing", reached),
        ):
            for spelling in explicit:
                try:
                    Store.open(db, keys_dir=spelling, read_only=True)
                except ReachedRing as done:
                    rows.append(
                        {"explicit": _points(spelling), "out": _selected(done.args[0])}
                    )
                else:
                    raise AssertionError(
                        "the signed reader must construct its key ring"
                    )

    homes = ("/fixture-profile", "relative-profile", ".", "", "~unavailable")
    xdgs = ("/fixture-config///", " \u2003//config/.././selected\u3000 ")
    overrides = (
        None,
        "",
        " ",
        ".",
        "a/../b",
        "//relative/name",
        "///top",
        "~/keys",
        "~/a/../keys",
        "./~/./x",
        "~bettermemory_invalid_reference_user_x/keys",
        "a\udcff",
    )
    with patch.dict(os.environ):
        for i, k in enumerate(overrides):
            for j in range(3):
                h = homes[(i + j) % len(homes)]
                x = xdgs[(i + j) % len(xdgs)]
                os.environ["HOME"] = h
                os.environ["XDG_CONFIG_HOME"] = x
                if k is None:
                    os.environ.pop("BETTERMEMORY_KEYS_DIR", None)
                else:
                    os.environ["BETTERMEMORY_KEYS_DIR"] = k
                try:
                    outcome = _selected(store_schema.resolve_keys_dir())
                except (RuntimeError, ValueError, OSError) as exc:
                    outcome = {
                        "raised": type(exc).__name__,
                        "message": _points(str(exc)),
                    }
                rows.append(
                    {
                        "home": _points(h),
                        "xdg": _points(x),
                        "override": _points(k),
                        "out": outcome,
                    }
                )
        for x in xdgs:
            os.environ["XDG_CONFIG_HOME"] = x
            os.environ["BETTERMEMORY_KEYS_DIR"] = "~unused-account/key"
            rows.append(
                {"default_xdg": _points(x), "out": _selected(log.default_keys_dir())}
            )
    return jsonl(rows)


def in_fresh_process(module: str) -> bytes:
    # Pytest replaces log.default_keys_dir and platformdirs for its own clients.
    # Resolve the real functions in a fresh process, under a disposable home.
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw).resolve()
        env = dict(os.environ)
        env.pop("PYTHONOPTIMIZE", None)
        env.update(
            HOME=str(scratch),
            TMPDIR=str(scratch),
            XDG_CONFIG_HOME=str(scratch / "config"),
            BETTERMEMORY_DIR=str(scratch / "store"),
            BETTERMEMORY_KEYS_DIR=str(scratch / "ring"),
            BETTERMEMORY_STATE_DIR=str(scratch / "state"),
            PYTHONPATH=os.pathsep.join(
                filter(None, (str(repo), str(repo / "src"), env.get("PYTHONPATH")))
            ),
        )
        data = subprocess.check_output(
            [sys.executable, "-m", module],
            cwd=repo,
            env=env,
        )
    return data


def generate() -> dict[str, bytes]:
    return {"key_directory_posix.jsonl": in_fresh_process(__name__)}


if __name__ == "__main__":
    sys.stdout.buffer.write(_from_store())
