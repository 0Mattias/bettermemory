"""The shim and roots carry exact native directory data over UTF-8 transports."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from urllib.parse import quote_from_bytes

import httpx2
import pytest

from bettermemory import identity, shim

from .test_identity import _no_ambient_identity

__all__ = ["_no_ambient_identity"]


@pytest.mark.parametrize(
    "path",
    [
        "/work/bytes-" + chr(0xDCFF),
        "/work/caf\u00e9",
        "/work/trailing\n",
        "/work/space ",
    ],
)
def test_shim_workspace_uses_ascii_headers_and_retains_the_exact_path(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(os, "getcwd", lambda: path)
    headers = shim._forward_headers(None)
    wire = httpx2.Headers(headers)
    context = SimpleNamespace(request=SimpleNamespace(headers=wire))
    caller = identity.resolve(context, environ={})
    assert caller.workspace.path == path
    assert caller.workspace.source == identity.SOURCE_HEADER
    assert json.loads(headers[identity.HEADER_WORKSPACE_JSON]) == path


@pytest.mark.parametrize(
    "value",
    [
        '"a\\u0000b"',
        "[]",
        "null",
        "{",
        '"' + "x" * (identity.MAX_DECLARED_LEN + 1) + '"',
    ],
)
def test_bad_encoded_workspace_is_dropped(value: str) -> None:
    assert identity._json_workspace(value) is None


def test_file_uri_root_preserves_percent_encoded_native_filename_bytes() -> None:
    raw = b"/work/bytes-\xff"
    uri = "file://" + quote_from_bytes(raw, safe="/")
    path = identity._first_file_root([SimpleNamespace(uri=uri)])
    assert path == "/work/bytes-" + chr(0xDCFF)
