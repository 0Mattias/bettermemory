"""Immutable source layout, bounded object reads and exact byte framing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bettermemory import _git, gitfile_rule, verify_layout
from bettermemory.claims import _MAX_MODULE_BYTES, parse_claim
from bettermemory.origin_patch import Change
from bettermemory.verify_layout import (
    _read_blobs,
    _single_line_assignments,
    literal_floor,
)
from bettermemory.verify_spelling import Spelled


@pytest.mark.parametrize(
    "source, expected",
    [
        (b"X = 30\r\nY = 3\r\n", {"X", "Y"}),
        (b"noise=0; X=30\n", {"noise"}),
        (b"X = (\n 30\n)\nX=30\n", set()),
        (b"X=30\nX=(\n 40\n)\n", {"X"}),
        (b"X: int=30\n", set()),
        (b"def f():\n X=30\n", set()),
        (b"X = 30\0\n", set()),
    ],
)
def test_only_the_first_supported_column_zero_assignment_counts(source, expected):
    assert _single_line_assignments(source) == expected


def _batch(blobs: dict[str, bytes]) -> bytes:
    return b"".join(
        f"{sha} blob {len(data)}\n".encode("ascii") + data + b"\n"
        for sha, data in sorted(blobs.items())
    )


def test_batch_framing_preserves_non_utf8_crlf_and_header_like_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = "b" * 40
    bodies = {"a" * 40: f"{other} blob 1\n".encode() + b"\xff\r\n\0", other: b"x"}
    raw = _batch(bodies)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(["git"], 0, stdout=raw)

    monkeypatch.setattr(gitfile_rule, "discovery_refusal", lambda _: None)
    monkeypatch.setattr(_git, "run_git", run)
    assert (
        _read_blobs(tmp_path, set(bodies), {s: len(b) for s, b in bodies.items()})
        == bodies
    )
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["text"] is False
    assert kwargs["stdin"] == b"a" * 40 + b"\n" + b"b" * 40 + b"\n"


@pytest.mark.parametrize("corruption", ["tail", "header", "short", "identity"])
def test_an_unreadable_batch_keeps_no_interpreted_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    sha = "a" * 40
    raw = _batch({sha: b"X=30\n"})
    if corruption == "tail":
        raw += b"extra"
    elif corruption == "header":
        raw = raw.replace(b" blob ", b" tree ", 1)
    elif corruption == "short":
        raw = raw[:-2]
    else:
        raw = b"b" * 40 + raw[40:]
    monkeypatch.setattr(gitfile_rule, "discovery_refusal", lambda _: None)
    monkeypatch.setattr(
        _git,
        "run_git",
        lambda *a, **k: subprocess.CompletedProcess(["git"], 0, stdout=raw),
    )
    before = _git.failed_calls()
    assert _read_blobs(tmp_path, {sha}, {sha: 5}) is None
    assert _git.failed_calls() > before


@pytest.mark.parametrize("missing", [True, False])
def test_unknown_or_oversized_sources_keep_the_files_exact_touch_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    blob, sha = "a" * 40, "b" * 40
    held = {} if missing else {blob: _MAX_MODULE_BYTES + 1}
    monkeypatch.setattr(
        verify_layout,
        "_listed",
        lambda *a: ({sha: [Change("mod.py", "M", (blob,))]}, held),
    )
    monkeypatch.setattr(verify_layout, "_held", lambda *a: {})

    def forbidden(*args, **kwargs):
        raise AssertionError("an unavailable or oversized source was read")

    monkeypatch.setattr(verify_layout, "_read_blobs", forbidden)
    found = literal_floor(
        tmp_path,
        [sha],
        ["mod.py"],
        ["mod.py"],
        [parse_claim("mod.py::X=30")],
        Spelled(),
        toplevel=tmp_path,
    )
    assert found is not None
    assert found.paths == {"mod.py"} and found.commits == {sha}


def test_a_loose_blob_arriving_under_the_same_head_recovers_the_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob, sha = "a" * 40, "b" * 40
    data = b"X=30\n"
    monkeypatch.setattr(
        verify_layout,
        "_listed",
        lambda *a: ({sha: [Change("mod.py", "M", (blob,))]}, {}),
    )
    monkeypatch.setattr(verify_layout, "_held", lambda *a: {blob: len(data)})
    monkeypatch.setattr(verify_layout, "_read_blobs", lambda *a: {blob: data})
    found = literal_floor(
        tmp_path,
        [sha],
        ["mod.py"],
        ["mod.py"],
        [parse_claim("mod.py::X=30")],
        Spelled(),
        toplevel=tmp_path,
    )
    assert found is not None and not found.paths and not found.commits
