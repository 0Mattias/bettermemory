"""Recorded key-ring path spellings, their native encodings and lexical equality."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bettermemory import log

from ._common import jsonl

_PATHS = (
    "",
    ".",
    "relative/leaf",
    "/",
    "//",
    "///a/./x///",
    "/aa/./b/../c",
    "/é/😀",
    "/x\0z",
    "/raw/\udc80",
    "/raw/\udcff",
    "/raw/\udcc3\udca9",
    "/raw/é",
    "/raw/\udcfe\udcff",
    "/raw/\ud800",
    "/raw/\udfff",
    "/raw/\udc7f",
    "/raw/\ud800\udcff",
    "/raw/\udc80\ud800",
    "/raw/\udcc3é\udca9",
    "/raw/😀/\udc80",
    "/raw/\ufffd",
    "C:",
    "C:relative",
    "C:/",
    "C:/foo/./y/..",
    "C:/raw/\udcff",
    "c:/RAW/\ud800",
    "é:/raw/\udc80",
    "\ud800:/x",
    "C:/😀/x",
    "C:/raw/\udcc3\udca9",
    "C:/raw/é",
    r"\rooted",
    r"\\server",
    r"\\server\share",
    r"\\server\share\folder",
    "\\\\server\\\ud800\\folder",
    "\\\\\udcff\\share\\f",
    r"\\?\UNC\server\share\foo",
    r"\\?\C:\x",
    r"\\.\pipe\thing",
    "/////",
    "///",
    "//a/./",
    "./C:/a",
    "C:/NUL.txt",
    "C:/file:stream",
    "C:/trailing. ",
    "C:/ß",
    "C:/SS",
    "c:/ẞ",
    "C:/İ",
    "C:/i\u0307",
    "C:/\U000f0000\U00100000\ud800",
)
_CONTROLS = (
    b"{",
    b"[]",
    b"{}",
    b'{"path":7}',
    b'{"path":null}',
    b'{"path":false}',
    b'{"path":"/a","path":"relative"}',
    b'{"path":"relative","path":"/a"}',
    b'\xef\xbb\xbf{"path":"/a"}',
    b'{"path":"/bad\xff"}',
    b'{\r\n"path":"/a"\r}',
)


def generate() -> dict[str, bytes]:
    rows: list[object] = []
    with TemporaryDirectory() as folder:
        ring = log.KeyRing(Path(folder), "0" * 32)
        file = ring.binding_path()
        for windows, pure in ((False, PurePosixPath), (True, PureWindowsPath)):
            inputs = [json.dumps({"path": path}).encode("ascii") for path in _PATHS]
            for raw in [*inputs, *_CONTROLS]:
                file.write_bytes(raw)
                with patch.object(log, "Path", pure):
                    result = ring.read_binding()
                text = None if result is None else str(result)
                rows.append(
                    [windows, list(raw), None if text is None else list(map(ord, text))]
                )
            for depth in (1500, 20_000):
                raw = (
                    b'{"data":' + b"[" * depth + b"0" + b"]" * depth + b',"path":"/x"}'
                )
                file.write_bytes(raw)
                try:
                    with patch.object(log, "Path", pure):
                        result = ring.read_binding()
                except RecursionError as exc:
                    outcome: object = {"recursion": str(exc)}
                else:
                    outcome = None if result is None else list(map(ord, str(result)))
                rows.append([windows, {"depth": depth}, outcome])
    return {"ring_binding_paths.jsonl": jsonl([{"rows": len(rows)}, *rows])}


def generate_codecs() -> dict[str, bytes]:
    rows: list[object] = []
    for windows, prefix in ((False, "/"), (True, "C:/")):
        for item in (
            "é",
            "\udcc3\udca9",
            "🌿",
            "\udcf0\udc9f\udc8c\udcbf",
            "\udc80",
            "\udc7f",
            "\ud800",
            "\udfff",
            "\ufffd",
            "ß",
            "SS",
            "ẞ",
            "İ",
            "i\u0307",
            "a/../b",
            "nested/./leaf///",
            "a very long path" * 20,
        ):
            pure = PureWindowsPath if windows else PurePosixPath
            path = pure(prefix + item)
            text = str(path)
            try:
                raw = (
                    text.encode("utf-16-le", "surrogatepass")
                    if windows
                    else text.encode("utf-8", "surrogateescape")
                )
            except UnicodeEncodeError:
                raw = None
            same = [
                (other, path == pure(prefix + other))
                for other in ("é", "\udcc3\udca9", "SS", "ß", "ẞ", "i\u0307", "é/../a")
            ]
            rows.append(
                [
                    windows,
                    list(map(ord, prefix + item)),
                    None if raw is None else list(raw),
                    list(json.dumps({"path": text}).encode("ascii")),
                    [[list(map(ord, prefix + other)), yes] for other, yes in same],
                ]
            )
    return {"ring_binding_path_codecs.jsonl": jsonl([{"rows": len(rows)}, *rows])}
