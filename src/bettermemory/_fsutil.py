"""Filesystem helpers — fsync durability + bounded reads.

Two concerns live here, both narrow:

**Durability (`fsync_file`, `fsync_dir`).** The store's write path is
`write to .tmp → rename to target`. POSIX guarantees the rename itself
is atomic, but the rename only updates the directory entry; the page
cache holding the file's bytes (and the directory's own metadata) can
still be lost on power loss between the rename returning and the kernel
flushing dirty pages. Without an explicit fsync we can end up with a
zero-byte file at the target path after an ungraceful shutdown — the
directory entry says the file exists, the data backing it never reached
disk.

* `fsync_file(fd)` — flush a file's data to disk. Cheap. The caller
  passes the open file descriptor (typically `f.fileno()` after
  `f.flush()` and before `close`).
* `fsync_dir(path)` — flush a directory's metadata so a rename inside
  it is durable. POSIX-only; on Windows you can't `open()` a directory
  for fsync and the OS handles rename durability differently anyway,
  so we no-op there.

Both fsync helpers swallow `OSError` and return. fsync legitimately
fails on some pseudo-filesystems (`/proc`, certain tmpfs/overlayfs
configs, sandbox mounts) where the operation isn't supported — that's
not a corruption risk, and propagating the error would break the write
surface for test/CI/container environments without buying any
durability they actually have.

**Bounded reads (`bounded_read`, `bounded_tail_read`,
`bounded_stream_read`).** Single point of enforcement for resource
caps on input we don't trust to be small. The 2.6.2 and 2.6.3 releases
fixed three separate unbounded-read defects (consolidate transcript,
hook transcript, llm.py contradiction clustering byte-vs-char trap);
the underlying class is one we keep producing because each call site
re-derives its own `.read(N)` discipline. Centralising it here means
the *next* time someone adds a "read this user-controlled file"
helper, the cap honours bytes (not characters), the error path is
named (`ValueError`, not OOM), and the byte-vs-char trap is impossible
because the helpers open in binary mode.

* `bounded_read(path, max_bytes)` — read a file's full contents in
  binary mode; raise `ValueError` if the file exceeds the cap. Stat-
  checks first so an over-cap file is rejected before the allocation.
  Use for files where over-cap is a malformed-input signal (config,
  frontmatter, JSON payloads).
* `bounded_tail_read(path, max_bytes)` — read up to the trailing
  `max_bytes` of a file; truncate silently and discard the first
  partial line. Falls back to a bounded forward read on unseekable
  streams (FIFOs from `mkfifo`-based fixtures, named pipes). Use for
  append-only logs where the head is uninteresting (transcripts,
  event-log tails).
* `bounded_stream_read(stream, max_bytes)` — read up to `max_bytes`
  from an open binary stream and raise `ValueError` if the stream
  carries more. Detects over-cap by attempting one extra byte after
  the limit. Use for stdin and other unseekable streams the caller
  has already opened.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO


def fsync_file(fd: int) -> None:
    """fsync a file descriptor. Best-effort — swallows OSError.

    Call after `flush()` and before `close()` on the writer. The kernel
    is free to ignore the fsync on filesystems that don't support it
    (proc, some tmpfs); we treat that as "as durable as this filesystem
    gets" rather than an error.
    """
    with contextlib.suppress(OSError):
        os.fsync(fd)


def fsync_dir(path: Path) -> None:
    """fsync a directory so a rename inside it is durable.

    POSIX requires `fsync` on the parent directory after a rename to
    guarantee the new directory entry survives a crash. On Windows you
    cannot open a directory with `os.open` for fsync, and NTFS handles
    rename ordering through its journal rather than user-issued fsync,
    so the helper is a no-op there.

    The `O_RDONLY` open is the cross-Unix portable form. Some
    filesystems (FAT, overlayfs in unprivileged user namespaces) reject
    directory fsync with EINVAL — we suppress and move on; the
    write/rename has already happened.
    """
    if sys.platform == "win32":  # pragma: no cover - non-unix
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def bounded_read(path: Path, max_bytes: int) -> bytes:
    """Read a file's full contents in binary mode, capped at ``max_bytes``.

    Raises ``ValueError`` if the file's size exceeds the cap. The stat
    check happens before the allocation so an oversized file is
    rejected without ever loading it. Returns bytes; callers decode
    explicitly (``.decode("utf-8")`` for user-controlled text,
    ``json.loads`` for structured input).

    A stat/open failure propagates as its native ``OSError`` subclass
    (``FileNotFoundError``, ``PermissionError``, …) unchanged — NOT
    re-wrapped into a bare ``OSError``. Callers special-case the
    subclass: ``Store.restore`` / ``Store.rename_scope`` catch
    ``FileNotFoundError`` to turn a vanished-file race into a clean
    ``MemoryNotFoundError``; flattening the subclass would silently
    make those handlers dead code.

    Use for inputs where over-cap is a malformed-input signal —
    frontmatter, config files, JSON payloads. For append-only logs
    where truncation is the right behaviour, use
    :func:`bounded_tail_read` instead.
    """
    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"{path}: file size {size} exceeds cap {max_bytes} bytes")
    with path.open("rb") as fh:
        return fh.read(max_bytes)


def bounded_tail_read(path: Path, max_bytes: int) -> bytes:
    """Read up to the trailing ``max_bytes`` of a file.

    Truncates silently — the use case is "I want the tail of an
    append-only log; the head is uninteresting." Returns bytes.
    Discards the first partial line in the chunk: when the seek lands
    mid-line, the bytes before the first newline are dropped so the
    caller never sees half a record. If no newline exists in the read
    window the whole chunk is returned (a single huge unbroken line
    will not have a partial-line discard applied — that's the caller's
    responsibility to handle).

    Falls back to a bounded forward read when the open file can't be
    seeked; the partial-line discard does not apply in that case
    because the read starts at the head. NOTE: the fallback only
    helps once the file is open — opening a FIFO with no writer
    blocks indefinitely, so a caller passing a possibly-non-regular
    path must guard with ``Path.is_file()`` first (see
    ``hook._extract_last_exchange`` and ``consolidate._load_transcript``).
    """
    with path.open("rb") as fh:
        try:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            offset = max(0, size - max_bytes)
            fh.seek(offset)
        except OSError:
            offset = 0
        chunk = fh.read(max_bytes)
    if offset > 0:
        newline = chunk.find(b"\n")
        if newline != -1:
            chunk = chunk[newline + 1 :]
    return chunk


@contextlib.contextmanager
def flock_excl(path: Path) -> Iterator[None]:
    """POSIX ``flock``-based mutual exclusion on ``path.lock``.

    The lockfile is created (or opened) at ``path.with_suffix(suffix +
    ".lock")`` and held under ``LOCK_EX`` for the duration of the
    ``with`` block. The lockfile is NOT unlinked on release — see the
    2.6.3 audit note: ``flock`` identity is per-inode; unlinking on
    release lets a third opener race in between the holder's
    ``os.open`` and a fresh ``O_CREAT``, ending up with two holders on
    different inodes that both believe they own the lock. Persisting
    the 0-byte lockfile keeps every ``os.open(lock_path, O_CREAT)``
    on the same inode so the flock actually serialises.

    The lockfile is created with ``0o600`` mode so the cross-host
    ``sync push`` posture doesn't leak it as world-readable
    (it's a zero-byte file, but a stray world-readable file in
    ``~/.claude-memory/`` is still bad form).

    No-op on Windows — ``fcntl`` is POSIX-only. The MVP single-process
    assumption applies there; callers using this for cross-process
    coordination get a degenerate one-process locker on Windows.

    This is the SINGLE definition. ``store.py``, ``events.py``, and
    ``sync.py`` all alias to this so a future fix to the locking
    discipline lands in one place and not three — see the 2.6.3
    pattern-generalization audit note.
    """
    if sys.platform == "win32":  # pragma: no cover - non-unix
        yield
        return

    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def bounded_stream_read(stream: BinaryIO, max_bytes: int) -> bytes:
    """Read up to ``max_bytes`` from an open binary stream.

    Raises ``ValueError`` if the stream carries more than the cap.
    Detection works by reading ``max_bytes + 1``: if the read returns
    exactly that many bytes, the stream has more available and the
    input is over-cap. The +1 byte is discarded on the error path.

    Use for unseekable streams the caller has opened — typically
    ``sys.stdin.buffer`` in CLI entry points where a pipe writer
    upstream might misbehave or stream gigabytes by accident.
    """
    raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"stream input exceeds cap {max_bytes} bytes")
    return raw


__all__ = [
    "fsync_file",
    "fsync_dir",
    "flock_excl",
    "bounded_read",
    "bounded_tail_read",
    "bounded_stream_read",
]
