"""Tests for `_fsutil` bounded-read helpers.

The fsync helpers are exercised indirectly by the durability suite. The
bounded-read helpers are new in 2.6.4 and are the single point of
enforcement for the byte-vs-char and unbounded-read defects the 2.6.x
audit cycle keeps surfacing — the tests pin the contract explicitly so
the next caller can't re-derive a broken cap.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from bettermemory._fsutil import (
    bounded_read,
    bounded_stream_read,
    bounded_tail_read,
)


class TestBoundedRead:
    def test_under_cap_returns_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_bytes(b"hello")
        assert bounded_read(path, max_bytes=1024) == b"hello"

    def test_exactly_at_cap_succeeds(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_bytes(b"x" * 100)
        assert bounded_read(path, max_bytes=100) == b"x" * 100

    def test_over_cap_raises_before_allocation(self, tmp_path: Path) -> None:
        path = tmp_path / "f.txt"
        path.write_bytes(b"x" * 1024)
        with pytest.raises(ValueError, match="exceeds cap"):
            bounded_read(path, max_bytes=512)

    def test_byte_not_char_cap(self, tmp_path: Path) -> None:
        """The classic 2.6.3 byte-vs-char trap: 4-byte UTF-8 codepoint
        repeated to fill the file. If the cap counted chars, a 2-byte
        cap would accept this. It must count bytes."""
        path = tmp_path / "f.txt"
        # 100 copies of a 4-byte codepoint (U+1F600 grinning face) = 400 bytes.
        path.write_text("\U0001f600" * 100, encoding="utf-8")
        assert path.stat().st_size == 400
        # Cap of 200 bytes must reject this even though it's only 100 chars.
        with pytest.raises(ValueError, match="exceeds cap"):
            bounded_read(path, max_bytes=200)
        # Cap of 400 bytes accepts it.
        assert bounded_read(path, max_bytes=400) == b"\xf0\x9f\x98\x80" * 100

    def test_missing_file_raises_oserror(self, tmp_path: Path) -> None:
        with pytest.raises(OSError, match="cannot stat"):
            bounded_read(tmp_path / "nope", max_bytes=1024)

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "empty"
        path.write_bytes(b"")
        assert bounded_read(path, max_bytes=1024) == b""


class TestBoundedTailRead:
    def test_under_cap_returns_full_file(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_bytes(b"line1\nline2\n")
        assert bounded_tail_read(path, max_bytes=1024) == b"line1\nline2\n"

    def test_over_cap_returns_tail(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_bytes(b"line1\nline2\nline3\nline4\n")
        # 10-byte cap → seeks to size-10 = 14; reads from offset 14.
        # File bytes 14..24 = "line4\n" preceded by half of "line3\n".
        # Partial-line discard drops the half-line; tail starts after the
        # first newline.
        result = bounded_tail_read(path, max_bytes=10)
        # Result must begin after a newline (no partial line at the head).
        assert b"\n" not in result or result.startswith(b"line")
        assert result.endswith(b"line4\n")

    def test_partial_line_discard_at_seek_boundary(self, tmp_path: Path) -> None:
        """When the seek lands inside a line, the partial head is
        dropped before returning."""
        path = tmp_path / "log.jsonl"
        # 100-byte content, 10 lines of "0123456789\n" each (11 bytes).
        path.write_bytes(b"0123456789\n" * 10)
        # Cap of 25 bytes → seek lands at byte 85, mid-line. After
        # partial-line discard, result should start at the next newline.
        result = bounded_tail_read(path, max_bytes=25)
        # Every retained line is a full record.
        assert all(line == b"0123456789" for line in result.splitlines() if line)

    def test_unseekable_stream_falls_back_to_forward_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FIFO / pipe paths can't seek; the helper must read forward
        up to the cap rather than crashing."""
        path = tmp_path / "log.jsonl"
        path.write_bytes(b"line1\nline2\n")
        real_open = Path.open

        def patched_open(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
            fh = real_open(self, *args, **kwargs)

            def fail_seek(*_a, **_kw):  # type: ignore[no-untyped-def]
                raise OSError("unseekable")

            fh.seek = fail_seek  # type: ignore[method-assign]
            return fh

        monkeypatch.setattr(Path, "open", patched_open)
        result = bounded_tail_read(path, max_bytes=1024)
        # Fallback reads from current position (start), full content fits.
        assert result == b"line1\nline2\n"

    def test_no_newline_returns_chunk_unchanged(self, tmp_path: Path) -> None:
        """A single huge unbroken line has no partial-line discard
        applied — the caller has to handle it."""
        path = tmp_path / "blob"
        path.write_bytes(b"x" * 200)
        # Cap of 50 → seek to 150, read 50 bytes, no newline to anchor
        # the partial-line discard, return as-is.
        result = bounded_tail_read(path, max_bytes=50)
        assert result == b"x" * 50

    def test_byte_not_char_cap(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        # Mix of single-byte and 4-byte codepoints to make sure the cap
        # operates on bytes regardless of UTF-8 width.
        body = ("a\U0001f600\n" * 50).encode("utf-8")  # 6 bytes * 50 = 300
        path.write_bytes(body)
        result = bounded_tail_read(path, max_bytes=60)
        # 60 bytes ≤ result ≤ 60 bytes (no partial-line discard adds bytes).
        assert len(result) <= 60


class TestBoundedStreamRead:
    def test_under_cap_returns_full(self) -> None:
        stream = io.BytesIO(b"hello world")
        assert bounded_stream_read(stream, max_bytes=1024) == b"hello world"

    def test_exactly_at_cap_succeeds(self) -> None:
        stream = io.BytesIO(b"x" * 100)
        assert bounded_stream_read(stream, max_bytes=100) == b"x" * 100

    def test_over_cap_raises(self) -> None:
        stream = io.BytesIO(b"x" * 200)
        with pytest.raises(ValueError, match="exceeds cap"):
            bounded_stream_read(stream, max_bytes=100)

    def test_empty_stream(self) -> None:
        stream = io.BytesIO(b"")
        assert bounded_stream_read(stream, max_bytes=1024) == b""

    def test_fifo_unseekable(self, tmp_path: Path) -> None:
        """Real FIFO smoke test — bounded_stream_read should work
        against an unseekable stream because it never seeks."""
        if not hasattr(os, "mkfifo"):  # pragma: no cover - non-unix
            pytest.skip("os.mkfifo not available")
        fifo = tmp_path / "pipe"
        os.mkfifo(fifo)
        pid = os.fork()
        if pid == 0:  # child
            try:
                with fifo.open("wb") as f:
                    f.write(b"hello from fifo")
            finally:
                os._exit(0)
        try:
            with fifo.open("rb") as f:
                result = bounded_stream_read(f, max_bytes=1024)
            assert result == b"hello from fifo"
        finally:
            os.waitpid(pid, 0)
