"""Claim 47: a `file://` root holding %00 crashes `origin.capture`.

docs/api.md: "Declared values are bounded client input: ... no control
characters ...; anything else is dropped, not stored." `identity.resolve`:
"Never raises: a channel that cannot be read is a channel that did not
answer." The header and env channels drop such a path (`_clean`); the roots
channel hands it to `origin.capture`, which must not raise for it.
"""

from __future__ import annotations

import os
from typing import Any
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from bettermemory import identity, origin
from bettermemory.identity import SOURCE_PROCESS_CWD, SOURCE_ROOTS

from ._mcp import fake_ctx
from .test_identity import _no_ambient_identity

__all__ = ["_no_ambient_identity"]


class _Roots:
    def __init__(self, uris: list[str]) -> None:
        self.uris = uris

    def check_client_capability(self, capability: Any) -> bool:
        return True

    async def list_roots(self) -> Any:
        from mcp import types

        return types.ListRootsResult(roots=[types.Root(uri=u) for u in self.uris])


async def _capture_under(uri: str | list[str]) -> Any:
    ctx = fake_ctx()
    ctx.request_context.session = _Roots([uri] if isinstance(uri, str) else uri)
    ctx.request_context.method = "tools/call"
    seen: dict[str, Any] = {}

    async def call_next(rc: Any) -> dict[str, Any]:
        caller = identity.current()
        assert caller is not None
        seen["workspace"] = caller.workspace
        try:
            seen["origin"] = origin.capture()
        except Exception as exc:  # noqa: BLE001
            seen["raised"] = exc
        return {"ok": True}

    await identity.middleware(ctx.request_context, call_next)
    return seen


async def test_control_root_for_reference_a_missing_root_captures() -> None:
    seen = await _capture_under("file:///tmp/hunt-47-no-such-dir")
    assert "raised" not in seen
    assert seen["origin"].source == SOURCE_ROOTS


async def test_a_root_with_a_nul_byte_does_not_crash_capture() -> None:
    seen = await _capture_under("file:///tmp/x%00y")
    assert "raised" not in seen, (
        f"origin.capture() raised {seen.get('raised')!r} for workspace "
        f"{seen['workspace']!r}"
    )
    got = seen["origin"]
    # Either the root was dropped (the labelled cwd fallback) or the
    # origin is the null one; never a path with a control character.
    assert got.cwd is None or "\x00" not in got.cwd
    assert got.source in (SOURCE_PROCESS_CWD, SOURCE_ROOTS)


async def test_header_channel_drops_the_same_path() -> None:
    ctx = fake_ctx()
    ctx.request_context.request.headers[identity.HEADER_WORKSPACE] = "/tmp/x\x00y"
    ws = identity.resolve(ctx.request_context, environ={}).workspace
    assert ws.source == SOURCE_PROCESS_CWD


@pytest.mark.parametrize(
    "bad", ["\x00", "\t", "\n", "\r", "\x7f", "x" * (identity.MAX_DECLARED_LEN + 1)]
)
async def test_bad_root_is_skipped_for_a_later_usable_root(bad: str) -> None:
    seen = await _capture_under(
        ["file:///tmp/" + quote("x" + bad + "y", safe=""), "file:///tmp/usable"]
    )
    assert "raised" not in seen
    assert seen["workspace"] == identity.Workspace(
        path="/tmp/usable", source=SOURCE_ROOTS
    )


def test_root_keeps_exact_format_characters_and_file_uri_forms() -> None:
    for uri, path in [
        ("file:///tmp/family%E2%80%8Dname", "/tmp/family\u200dname"),
        ("file:///C:/work", "C:/work"),
        ("file://server/share/work", "//server/share/work"),
    ]:
        assert identity._first_file_root([SimpleNamespace(uri=uri)]) == path


def test_explicit_nul_cwd_declines_without_probing_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def probe(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid cwd must decline before a Git probe")

    monkeypatch.setattr(origin, "_git_result", probe)
    got = origin.capture(Path("/tmp/x\x00y"), source=SOURCE_ROOTS)
    assert got.cwd is None and got.repo is None and got.worktree_root is None
    assert got.source == SOURCE_ROOTS


def test_nul_cwd_is_refused_before_platform_path_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def resolve(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("a NUL path must be refused before platform resolution")

    monkeypatch.setattr(os.path, "realpath", resolve)
    got = origin.capture(Path("/tmp/x\x00y"), source=SOURCE_ROOTS)
    assert got.cwd is None and got.repo is None and got.worktree_root is None
    assert got.source == SOURCE_ROOTS
